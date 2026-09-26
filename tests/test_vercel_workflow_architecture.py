from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "vercel_runtime" / "plane_workflows.py"
OBSOLETE = ROOT / "vercel_runtime" / "plane_workflows_fixed.py"
INGRESS = ROOT / "vercel_runtime" / "api" / "index.py"
PYPROJECT = ROOT / "vercel_runtime" / "pyproject.toml"


def test_only_canonical_vercel_workflow_module_exists() -> None:
    assert CANONICAL.exists()
    assert not OBSOLETE.exists()


def test_vercel_ingress_and_packaging_use_canonical_workflow_module() -> None:
    ingress = INGRESS.read_text(encoding="utf-8")
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert "from plane_workflows import (" in ingress
    assert "plane_workflows_fixed" not in ingress
    assert 'entrypoint = "plane_workflows:wf"' in pyproject
    assert "plane_workflows_fixed" not in pyproject


def test_canonical_workflow_is_v33_graph_not_retired_v32_graph() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    assert 'Workflows(namespace="planev33")' in text
    assert 'Workflows(namespace="planev32")' not in text
    assert "process_telegram_update_v33" in text
    assert "monitor_cycle_step_v33" in text


def test_obsolete_workflow_filename_cannot_return_in_active_source() -> None:
    roots = (
        ROOT / "vercel_runtime",
        ROOT / ".github",
        ROOT / "scripts",
        ROOT / "tests",
    )
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.resolve() == this_file:
                continue
            if path.suffix not in {".py", ".toml", ".yml", ".yaml", ".json", ".md", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "plane_workflows_fixed" in text:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], "obsolete Vercel workflow reference(s): " + ", ".join(offenders)
