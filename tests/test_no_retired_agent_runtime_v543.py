from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_retired_agent_runtime_files_are_absent():
    forbidden = [
        "Dockerfile.retired_agent",
        "requirements-retired_agent.txt",
        "GeminiApiRateLimit.txt",
        "app/retired_agent_console.py",
        "app/retired_agent_permission_guard_v422.py",
        "app/retired_agent_prediction_bridge.py",
        "app/retired_agent_state.py",
        "app/retired_agent_tool_recovery_v431.py",
        "app/retired_agent_worker.py",
        "app/retired_agent_worker_ext.py",
        "scripts/retired_agent-worker-entrypoint.sh",
        "scripts/retired_agent_bridge_daemon.py",
        "scripts/retired_agent_record_finding.py",
    ]
    assert [path for path in forbidden if (ROOT / path).exists()] == []


def test_telegram_runtime_has_no_retired_agent_command_or_handler():
    source = (ROOT / "app/main.py").read_text(encoding="utf-8")
    assert 'BotCommand("retired_agent"' not in source
    assert "register_retired_agent_console_handlers" not in source
    assert "app.retired_agent_console" not in source


def test_configuration_has_no_retired_agent_bridge_settings():
    config = (ROOT / "app/config.py").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for marker in ("retired_agent_worker_url", "retired_agent_worker_token", "retired external agent_WORKER_URL", "retired external agent_WORKER_TOKEN"):
        assert marker not in config
        assert marker not in env_example


def test_normal_ci_has_no_retired_agent_jobs():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "test_retired_agent_" not in workflow
    assert "test_v53_retired_agent_regressions.py" not in workflow


def test_active_runtime_tree_has_no_precise_retired_agent_references():
    """Historical release notes may describe removal; executable/config code may not."""
    roots = [ROOT / "app", ROOT / "scripts", ROOT / ".github"]
    standalone = [ROOT / ".env.example", ROOT / "Dockerfile", ROOT / "requirements.txt"]
    candidates: list[Path] = []
    for root in roots:
        candidates.extend(path for path in root.rglob("*") if path.is_file())
    candidates.extend(path for path in standalone if path.exists())

    precise_markers = (
        "retired external agent",
        "app.retired_agent_",
        "from app.retired_agent",
        "import app.retired_agent",
        "retired_agent_worker",
        "retired_agent_console",
        "retired_agent_bridge",
        "retired_agent_state",
        "retired_agent_permission",
        "retired_agent_tool_recovery",
        "retired_agent-worker",
        "retired_agent-record",
        "retired_agent_",
        "/retired_agent",
        'botcommand("retired_agent"',
        "dockerfile.retired_agent",
        "requirements-retired_agent",
    )
    hits: list[str] = []
    for path in candidates:
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").casefold()
        if any(marker.casefold() in text for marker in precise_markers):
            hits.append(str(path.relative_to(ROOT)))
    assert hits == []
