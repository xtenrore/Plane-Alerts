from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "community_installer_v55.py"


def _installer():
    spec = importlib.util.spec_from_file_location("community_installer_v55", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v55_all_required_user_facing_installers_exist():
    required = (
        "setup.bat",
        "setup-windows.bat",
        "setup-windows-arm64.bat",
        "setup-linux.sh",
        "setup-linux-arm64.sh",
        "setup-raspberry-pi.sh",
    )
    for name in required:
        path = ROOT / name
        assert path.is_file() and path.stat().st_size > 100


def test_installer_self_test_and_stable_semver_parser():
    result = subprocess.run([sys.executable, str(SCRIPT), "--self-test"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "COMMUNITY_INSTALLER_SELF_TEST=ok" in result.stdout
    installer = _installer()
    assert installer.semver("v5.5.1") == (5, 5, 1)
    with pytest.raises(ValueError):
        installer.semver("v5.5.1-rc1")
    with pytest.raises(ValueError):
        installer.semver("main")


def test_latest_release_parser_refuses_prerelease_and_draft(monkeypatch):
    installer = _installer()
    monkeypatch.setattr(installer, "api_json", lambda *_a, **_k: {"tag_name": "v5.5.1", "prerelease": True, "draft": False})
    with pytest.raises(RuntimeError):
        installer.latest_stable_release()
    monkeypatch.setattr(installer, "api_json", lambda *_a, **_k: {"tag_name": "v5.5.1", "prerelease": False, "draft": True})
    with pytest.raises(RuntimeError):
        installer.latest_stable_release()


def test_env_render_is_atomic_friendly_and_secret_redaction_is_explicit(tmp_path):
    installer = _installer()
    template = tmp_path / ".env.example"
    template.write_text("TELEGRAM_BOT_TOKEN=placeholder\nDATABASE_BACKEND=mongodb\n", encoding="utf-8")
    rendered = installer.render_env(template, {"TELEGRAM_BOT_TOKEN": "secret", "DATABASE_BACKEND": "sqlite", "SQLITE_PATH": "/tmp/db"})
    assert "TELEGRAM_BOT_TOKEN=secret" in rendered
    assert "DATABASE_BACKEND=sqlite" in rendered
    assert "SQLITE_PATH=/tmp/db" in rendered
    assert installer.redact_value("TELEGRAM_BOT_TOKEN", "secret") == "***"
    assert installer.redact_value("LOCAL_ADSB_RECEIVER_LATITUDE", "41.0") == "***"


def test_community_env_defaults_to_real_sqlite_and_keeps_receiver_location_separate():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_BACKEND=sqlite" in text
    assert "SQLITE_PATH=" in text
    assert "LOCAL_ADSB_RECEIVER_LATITUDE=" in text
    assert "LOCAL_ADSB_RECEIVER_LONGITUDE=" in text
    assert "LOCAL_ADSB_RECEIVER_COVERAGE_KM=" in text
    assert "PREDICTION_LAB_ROOT=./.community/prediction_lab" in text


def test_community_runtime_wraps_receiver_guard_without_deployment_specific_code():
    runtime = (ROOT / "app" / "community_main_v55.py").read_text(encoding="utf-8")
    installer = SCRIPT.read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "install_local_receiver_coverage_guard()" in runtime
    assert runtime.index("install_local_receiver_coverage_guard()") < runtime.index("from app.main import app")
    assert "railway" not in installer.casefold()
    assert "app.main:app" in compose
    assert "DATABASE_BACKEND: mongodb" in compose
    assert "PREDICTION_LAB_ROOT: /data/prediction_lab" in compose


def test_updater_has_backup_validation_and_rollback_gates():
    text = SCRIPT.read_text(encoding="utf-8")
    for required in (
        "latest_stable_release",
        "backup_for_update",
        '["git", "status", "--porcelain"',
        "run_doctor(root, offline=True)",
        '["git", "checkout", "--detach"',
        "Rolling back",
        "restart_service()",
    ):
        assert required in text


def test_windows_dependency_lock_excludes_uvloop_only_on_windows():
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert 'uvloop==0.22.1; platform_system != "Windows"' in lock


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell syntax is gated on Linux CI; Windows CI gates the .bat entry points natively")
def test_shell_entrypoints_parse_and_use_stable_release_install_semantics():
    for name in ("setup-linux.sh", "setup-linux-arm64.sh", "setup-raspberry-pi.sh"):
        path = ROOT / name
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"
        text = path.read_text(encoding="utf-8")
        assert "--branch main" not in text
        assert "refs/heads/main" not in text


def test_raspberry_pi_platform_guard_can_be_exercised_without_spoofing_normal_runs():
    text = (ROOT / "setup-raspberry-pi.sh").read_text(encoding="utf-8")
    assert "PLANE_ALERTS_INSTALLER_TEST" in text
    assert "Raspberry Pi 4" in text and "Raspberry Pi 5" in text
    assert "64-bit" in text


def _make_local_release_repo(tmp_path: Path) -> tuple[Path, str, str]:
    def git(cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
        return result.stdout.strip()

    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    install = tmp_path / "install"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, text=True, capture_output=True)
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, text=True, capture_output=True)
    git(work, "config", "user.name", "Plane Alerts Community CI")
    git(work, "config", "user.email", "community-ci@example.invalid")
    (work / "tracked.txt").write_text("old release\n", encoding="utf-8")
    git(work, "add", "tracked.txt")
    git(work, "commit", "-m", "community v5.5.3 baseline")
    git(work, "branch", "-M", "main")
    old_commit = git(work, "rev-parse", "HEAD")
    git(work, "tag", "v5.5.3")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "main", "--tags")

    (work / "tracked.txt").write_text("new release\n", encoding="utf-8")
    git(work, "add", "tracked.txt")
    git(work, "commit", "-m", "community v5.8.8 candidate")
    new_commit = git(work, "rev-parse", "HEAD")
    git(work, "tag", "v5.8.8")
    git(work, "push", "origin", "main", "--tags")

    subprocess.run(["git", "clone", str(remote), str(install)], check=True, text=True, capture_output=True)
    git(install, "checkout", "--detach", "v5.5.3")
    return install, old_commit, new_commit


def test_updater_success_records_exact_stable_tag_and_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    installer = _installer()
    root, old_commit, new_commit = _make_local_release_repo(tmp_path)
    data_root = root / ".community"
    data_root.mkdir(parents=True, exist_ok=True)
    metadata_path = data_root / "installation.json"
    checkpoint_path = data_root / "setup-state.json"
    metadata_path.write_text(json.dumps({"format": 1, "community": True, "repository": installer.REPO, "installed_tag": "v5.5.3", "installed_commit": old_commit, "update_policy": "manual"}), encoding="utf-8")
    checkpoint_path.write_text(json.dumps({"step": "ready", "secrets": "not-stored"}), encoding="utf-8")
    (root / ".env").write_text("DATABASE_BACKEND=mongodb\nMONGO_URI=mongodb://127.0.0.1:27017\n", encoding="utf-8")

    monkeypatch.setattr(installer, "latest_stable_release", lambda: {"tag_name": "v5.8.8", "draft": False, "prerelease": False})
    monkeypatch.setattr(installer, "stop_service", lambda: None)
    monkeypatch.setattr(installer, "install_dependencies", lambda _root: None)
    monkeypatch.setattr(installer, "run_doctor", lambda _root, offline=False: True)
    monkeypatch.setattr(installer, "restart_service", lambda: None)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    assert installer.update(root) is True
    saved = json.loads(metadata_path.read_text(encoding="utf-8"))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert installer.git_head(root) == new_commit
    assert installer.current_tag(root) == "v5.8.8"
    assert saved["installed_tag"] == "v5.8.8"
    assert saved["installed_commit"] == new_commit
    assert saved["verified_at"]
    assert checkpoint["step"] == "ready"
    assert checkpoint["secrets"] == "not-stored"


def test_updater_rolls_back_code_env_sqlite_and_metadata_if_final_state_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sqlite3

    installer = _installer()
    root, old_commit, _new_commit = _make_local_release_repo(tmp_path)
    data_root = root / ".community"
    database_path = root / "data" / "planealerts.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE state (value TEXT NOT NULL)")
        db.execute("INSERT INTO state(value) VALUES ('before-update')")

    original_env = "DATABASE_BACKEND=sqlite\nSQLITE_PATH=data/planealerts.db\nTELEGRAM_BOT_TOKEN=old-token\n"
    (root / ".env").write_text(original_env, encoding="utf-8")
    metadata_path = data_root / "installation.json"
    checkpoint_path = data_root / "setup-state.json"
    original_metadata = {"format": 1, "community": True, "repository": installer.REPO, "installed_tag": "v5.5.3", "installed_commit": old_commit, "update_policy": "manual"}
    original_checkpoint = {"step": "ready", "secrets": "not-stored"}
    metadata_path.write_text(json.dumps(original_metadata), encoding="utf-8")
    checkpoint_path.write_text(json.dumps(original_checkpoint), encoding="utf-8")

    monkeypatch.setattr(installer, "latest_stable_release", lambda: {"tag_name": "v5.8.8", "draft": False, "prerelease": False})
    monkeypatch.setattr(installer, "stop_service", lambda: None)
    doctor_results = iter([True, True, True])
    monkeypatch.setattr(installer, "run_doctor", lambda _root, offline=False: next(doctor_results))
    monkeypatch.setattr(installer, "restart_service", lambda: None)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)

    install_calls = 0
    def mutate_only_during_target_install(target_root: Path) -> None:
        nonlocal install_calls
        install_calls += 1
        if install_calls == 1:
            (target_root / ".env").write_text("DATABASE_BACKEND=sqlite\nSQLITE_PATH=changed.db\nTELEGRAM_BOT_TOKEN=new-token\n", encoding="utf-8")
            with sqlite3.connect(database_path) as db:
                db.execute("UPDATE state SET value='during-failed-update'")
    monkeypatch.setattr(installer, "install_dependencies", mutate_only_during_target_install)

    original_write = installer.write_json_atomic
    def fail_final_checkpoint(path: Path, payload: dict) -> None:
        if path == checkpoint_path and payload.get("step") == "ready":
            raise OSError("simulated final checkpoint persistence failure")
        original_write(path, payload)
    monkeypatch.setattr(installer, "write_json_atomic", fail_final_checkpoint)

    with pytest.raises(OSError, match="simulated final checkpoint persistence failure"):
        installer.update(root)

    assert installer.git_head(root) == old_commit
    assert installer.current_tag(root) == "v5.5.3"
    assert (root / ".env").read_text(encoding="utf-8") == original_env
    assert json.loads(metadata_path.read_text(encoding="utf-8")) == original_metadata
    assert json.loads(checkpoint_path.read_text(encoding="utf-8")) == original_checkpoint
    with sqlite3.connect(database_path) as db:
        assert db.execute("SELECT value FROM state").fetchone()[0] == "before-update"
    assert install_calls == 2


def test_updater_stops_service_before_snapshot_and_restarts_if_snapshot_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = _installer()
    root, old_commit, _new_commit = _make_local_release_repo(tmp_path)
    (root / ".env").write_text("DATABASE_BACKEND=sqlite\nSQLITE_PATH=data/planealerts.db\n", encoding="utf-8")
    data_root = root / ".community"
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "installation.json").write_text(
        '{"format":1,"community":true,"repository":"xtenrore/Plane-Alerts","installed_tag":"v5.5.3","installed_commit":"'
        + old_commit
        + '","update_policy":"manual"}',
        encoding="utf-8",
    )
    (data_root / "setup-state.json").write_text('{"step":"ready","secrets":"not-stored"}', encoding="utf-8")

    calls: list[str] = []
    monkeypatch.setattr(installer, "latest_stable_release", lambda: {"tag_name": "v5.8.8", "draft": False, "prerelease": False})
    monkeypatch.setattr(installer, "run_doctor", lambda _root, offline=False: True)
    monkeypatch.setattr(installer, "stop_service", lambda: calls.append("stop"))
    def fail_backup(_root: Path) -> Path:
        calls.append("backup")
        raise OSError("snapshot failed")

    monkeypatch.setattr(installer, "backup_for_update", fail_backup)
    monkeypatch.setattr(installer, "restart_service", lambda: calls.append("restart"))

    with pytest.raises(OSError, match="snapshot failed"):
        installer.update(root)

    assert calls == ["stop", "backup", "restart"]
    assert installer.git_head(root) == old_commit


def test_updater_refuses_tracked_local_changes_before_fetching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _installer()
    root, _old_commit, _new_commit = _make_local_release_repo(tmp_path)
    (root / "tracked.txt").write_text("user edit\n", encoding="utf-8")
    monkeypatch.setattr(installer, "latest_stable_release", lambda: {"tag_name": "v5.8.8", "draft": False, "prerelease": False})
    with pytest.raises(RuntimeError, match="Local changes were detected"):
        installer.update(root)


def test_external_setup_validators_cover_telegram_opensky_and_local_receiver(monkeypatch: pytest.MonkeyPatch) -> None:
    installer = _installer()
    seen: list[tuple[str, str]] = []

    def fake_api(url: str, *, method: str = "GET", data: bytes | None = None, **_kwargs):
        seen.append((url, method))
        if "api.telegram.org" in url:
            return {"ok": True, "result": {"username": "plane_alerts_test_bot"}}
        if "auth.opensky-network.org" in url:
            assert data and b"client_id=test-client" in data and b"client_secret=test-secret" in data
            return {"access_token": "mock-access-token"}
        if url.endswith("/data/aircraft.json"):
            return {"aircraft": [{"hex": "abc123"}]}
        return {}
    monkeypatch.setattr(installer, "api_json", fake_api)

    telegram_ok, telegram_detail = installer.validate_telegram("123456:mock-token")
    opensky_ok, opensky_detail = installer.validate_opensky("test-client:test-secret")
    receiver_ok, receiver_detail = installer.check_receiver("http://127.0.0.1:8080")

    assert telegram_ok and "@plane_alerts_test_bot" in telegram_detail
    assert opensky_ok and "OAuth2" in opensky_detail
    assert receiver_ok and "1" in receiver_detail
    assert len(seen) == 3


def test_support_bundle_redacts_credentials_and_receiver_coordinates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    installer = _installer()
    root = tmp_path / "install"
    root.mkdir()
    (root / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456:secret\nLOCAL_ADSB_RECEIVER_LATITUDE=41.1\nLOCAL_ADSB_RECEIVER_LONGITUDE=28.7\n",
        encoding="utf-8",
    )
    metadata_path = root / ".community" / "installation.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(json.dumps({"community": True, "update_policy": "manual"}), encoding="utf-8")
    monkeypatch.setattr(installer, "git_head", lambda _root: "a" * 40)
    monkeypatch.setattr(installer, "current_tag", lambda _root: "v5.8.8")

    bundle = installer.support_bundle(root)
    payload = json.loads(bundle.read_text(encoding="utf-8"))
    rendered = bundle.read_text(encoding="utf-8")
    assert payload["config"]["TELEGRAM_BOT_TOKEN"] == "***"
    assert payload["config"]["LOCAL_ADSB_RECEIVER_LATITUDE"] == "***"
    assert payload["config"]["LOCAL_ADSB_RECEIVER_LONGITUDE"] == "***"
    assert "123456:secret" not in rendered
    assert "41.1" not in rendered
    assert "28.7" not in rendered

def test_fresh_install_configures_sqlite_and_marks_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    installer = _installer()
    root = tmp_path / "fresh-install"
    root.mkdir()
    (root / ".env.example").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    calls: list[object] = []
    monkeypatch.setattr(installer, "install_dependencies", lambda _root: calls.append("dependencies"))
    monkeypatch.setattr(installer, "choose", lambda _prompt, _options, default=1: default)
    monkeypatch.setattr(installer, "prompt_secret", lambda *_args, **_kwargs: "123456:mock-community-token")
    monkeypatch.setattr(installer, "validate_telegram", lambda _token: (True, "Telegram mock authenticated"))
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        installer,
        "run_doctor",
        lambda _root, offline=False: calls.append(("doctor", offline)) or True,
    )
    monkeypatch.setattr(
        installer,
        "install_service",
        lambda _root, metadata: calls.append(("service", metadata["database_backend"])),
    )
    monkeypatch.setattr(installer, "current_tag", lambda _root: "v5.8.8")
    monkeypatch.setattr(installer, "git_head", lambda _root: "a" * 40)

    installer.install(root)

    values = installer.parse_env(root / ".env")
    assert values["DATABASE_BACKEND"] == "sqlite"
    assert values["SQLITE_PATH"].endswith(".community/data/planealerts.db")
    assert values["TELEGRAM_BOT_TOKEN"] == "123456:mock-community-token"
    metadata = json.loads((root / ".community" / "installation.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((root / ".community" / "setup-state.json").read_text(encoding="utf-8"))
    assert metadata["installed_tag"] == "v5.8.8"
    assert metadata["installed_commit"] == "a" * 40
    assert metadata["database_backend"] == "sqlite"
    assert "mock-community-token" not in json.dumps(metadata)
    assert checkpoint["step"] == "ready"
    assert checkpoint["secrets"] == "not-stored"
    assert "dependencies" in calls
    assert ("doctor", True) in calls
    assert ("doctor", False) in calls
    assert ("service", "sqlite") in calls

