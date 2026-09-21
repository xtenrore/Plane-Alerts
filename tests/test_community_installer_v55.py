from __future__ import annotations

import importlib.util
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
    assert "AGY=excluded" in result.stdout
    installer = _installer()
    assert installer.semver("v5.5.0") == (5, 5, 0)
    with pytest.raises(ValueError):
        installer.semver("v5.5.0-rc1")
    with pytest.raises(ValueError):
        installer.semver("main")


def test_latest_release_parser_refuses_prerelease_and_draft(monkeypatch):
    installer = _installer()
    monkeypatch.setattr(installer, "api_json", lambda *_a, **_k: {"tag_name": "v5.5.0", "prerelease": True, "draft": False})
    with pytest.raises(RuntimeError):
        installer.latest_stable_release()
    monkeypatch.setattr(installer, "api_json", lambda *_a, **_k: {"tag_name": "v5.5.0", "prerelease": False, "draft": True})
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
    assert "AGY_WORKER_URL=" not in text
    assert "AGY_WORKER_TOKEN=" not in text


def test_community_runtime_and_installer_cannot_start_agy():
    runtime = (ROOT / "app" / "community_main_v55.py").read_text(encoding="utf-8")
    installer = SCRIPT.read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert 'ModuleType("app.agy_console")' in runtime
    assert "register_agy_console_handlers = lambda" in runtime
    assert "Dockerfile.agy" not in installer
    assert "railway" not in installer.casefold()
    assert "from app.agy" not in installer
    assert "import app.agy" not in installer
    assert "agy_enabled\": False" in installer
    assert "app.community_main_v55:app" in compose
    assert "DATABASE_BACKEND: mongodb" in compose
    assert 'AGY_WORKER_URL: ""' in compose
    assert 'AGY_WORKER_TOKEN: ""' in compose


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


def test_shell_entrypoints_parse_and_are_agy_free():
    for name in ("setup-linux.sh", "setup-linux-arm64.sh", "setup-raspberry-pi.sh"):
        path = ROOT / name
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, f"{name}: {result.stderr}"
        text = path.read_text(encoding="utf-8")
        assert "AGY=excluded" in text
        assert "--branch main" not in text
        assert "refs/heads/main" not in text


def test_raspberry_pi_platform_guard_can_be_exercised_without_spoofing_normal_runs():
    text = (ROOT / "setup-raspberry-pi.sh").read_text(encoding="utf-8")
    assert 'PLANE_ALERTS_INSTALLER_TEST' in text
    assert 'Raspberry Pi 4' in text and 'Raspberry Pi 5' in text
    assert '64-bit' in text
