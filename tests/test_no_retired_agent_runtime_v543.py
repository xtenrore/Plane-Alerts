from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_retired_agent_runtime_files_are_absent():
    forbidden = [
        "Dockerfile.agy",
        "requirements-agy.txt",
        "GeminiApiRateLimit.txt",
        "app/agy_console.py",
        "app/agy_permission_guard_v422.py",
        "app/agy_prediction_bridge.py",
        "app/agy_state.py",
        "app/agy_tool_recovery_v431.py",
        "app/agy_worker.py",
        "app/agy_worker_ext.py",
        "scripts/agy-worker-entrypoint.sh",
        "scripts/agy_bridge_daemon.py",
        "scripts/agy_record_finding.py",
    ]
    assert [path for path in forbidden if (ROOT / path).exists()] == []


def test_telegram_runtime_has_no_retired_agent_command_or_handler():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'BotCommand("agy"' not in source
    assert "register_agy_console_handlers" not in source
    assert "app.agy_console" not in source


def test_configuration_has_no_retired_agent_bridge_settings():
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for marker in ("agy_worker_url", "agy_worker_token", "AGY_WORKER_URL", "AGY_WORKER_TOKEN"):
        assert marker not in config
        assert marker not in env_example


def test_normal_ci_has_no_retired_agent_jobs():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "test_agy_" not in workflow
    assert "test_v53_agy_regressions.py" not in workflow


def test_active_runtime_tree_has_no_retired_agent_references():
    """Historical release notes may describe removal; executable/config code may not."""
    roots = [ROOT / "app", ROOT / "scripts", ROOT / ".github"]
    standalone = [ROOT / ".env.example", ROOT / "Dockerfile", ROOT / "requirements.txt"]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(path for path in root.rglob("*") if path.is_file())
    candidates.extend(path for path in standalone if path.exists())

    hits: list[str] = []
    for path in candidates:
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if "agy" in text or "antigravity" in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
