from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_repository_pipeline_structure_and_durable_checkpoints_exist():
    expected = ("prediction_lab/raw", "prediction_lab/unchecked", "prediction_lab/reviewed", "prediction_lab/error_museum", "prediction_lab/archive/mongo-import", "prediction_lab/state", "prediction_lab/schemas")
    for rel in expected: assert (ROOT / rel).exists(), rel
    for name in ("collector", "investigator", "release"):
        state = json.loads((ROOT / "prediction_lab" / "state" / f"{name}.json").read_text(encoding="utf-8"))
        assert state["schema"] == "plane-alerts-pipeline-checkpoint-v1"
        assert state["task"] == name


def test_prediction_data_branch_cannot_trigger_production_deploy():
    deploy = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy-railway.yml").read_text(encoding="utf-8"))
    text = (ROOT / ".github" / "workflows" / "deploy-railway.yml").read_text(encoding="utf-8")
    assert "head_branch == 'main'" in text
    assert "prediction-lab-data" not in text
    assert deploy["permissions"]["contents"] == "read"


def test_sync_is_bounded_and_acknowledges_only_after_successful_push():
    text = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    assert "prediction-lab-data" in text
    assert 'MAX_FILES: "80"' in text and 'MAX_BYTES: "26214400"' in text
    assert "git pull --rebase" in text and "git push origin HEAD:prediction-lab-data" in text
    assert text.index(".synced") > text.index("git push origin HEAD:prediction-lab-data")
    assert "secrets.GITHUB_TOKEN" not in text


def test_pipeline_collect_is_idempotent(tmp_path: Path):
    raw = tmp_path / "prediction_lab" / "raw" / "2026-09-24"; raw.mkdir(parents=True)
    event = {"schema": "plane-alerts-prediction-evidence-v1", "case_id": "case-abc", "event_id": "evt-abc"}
    (raw / "evt-abc.json").write_text(json.dumps(event) + "\n", encoding="utf-8")
    script = ROOT / "scripts" / "prediction_lab_pipeline.py"
    cmd = [sys.executable, str(script), "--root", str(tmp_path / "prediction_lab"), "collect", "--limit", "10"]
    subprocess.run(cmd, check=True); subprocess.run(cmd, check=True)
    cases = list((tmp_path / "prediction_lab" / "unchecked").rglob("*.json"))
    assert len(cases) == 1
