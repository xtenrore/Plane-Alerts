from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEGACY = "a" + "gy"
LEGACY_UPPER = LEGACY.upper()
OLD_BRAND = "anti" + "gravity"


def test_removed_runtime_files_are_absent():
    forbidden = [
        f"Dockerfile.{LEGACY}",
        f"requirements-{LEGACY}.txt",
        "GeminiApiRateLimit.txt",
        f"app/{LEGACY}_console.py",
        f"app/{LEGACY}_permission_guard_v422.py",
        f"app/{LEGACY}_prediction_bridge.py",
        f"app/{LEGACY}_state.py",
        f"app/{LEGACY}_tool_recovery_v431.py",
        f"app/{LEGACY}_worker.py",
        f"app/{LEGACY}_worker_ext.py",
        f"scripts/{LEGACY}-worker-entrypoint.sh",
        f"scripts/{LEGACY}_bridge_daemon.py",
        f"scripts/{LEGACY}_record_finding.py",
    ]
    assert [path for path in forbidden if (ROOT / path).exists()] == []


def test_telegram_runtime_has_no_removed_command_or_handler():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8").casefold()
    assert f'botcommand("{LEGACY}"' not in source
    assert f"register_{LEGACY}_console_handlers" not in source
    assert f"app.{LEGACY}_console" not in source


def test_configuration_has_no_removed_bridge_settings():
    config = (ROOT / "app/config.py").read_text(encoding="utf-8").casefold()
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8").casefold()
    for marker in (f"{LEGACY}_worker_url", f"{LEGACY}_worker_token"):
        assert marker not in config
        assert marker not in env_example


def test_normal_ci_has_no_removed_jobs():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8").casefold()
    assert f"test_{LEGACY}_" not in workflow
    assert f"test_v53_{LEGACY}_regressions.py" not in workflow


def test_active_runtime_tree_has_no_precise_removed_integration_references():
    roots = [ROOT / "app", ROOT / "scripts", ROOT / ".github"]
    standalone = [ROOT / ".env.example", ROOT / "Dockerfile", ROOT / "requirements.txt"]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(path for path in root.rglob("*") if path.is_file())
    candidates.extend(path for path in standalone if path.exists())
    precise_markers = (LEGACY, LEGACY_UPPER, OLD_BRAND)
    hits: list[str] = []
    for path in candidates:
        if path.suffix == ".pyc" or "__pycache__" in path.parts or path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        rel = str(path.relative_to(ROOT)).casefold()
        if any(marker.casefold() in text or marker.casefold() in rel for marker in precise_markers):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
