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


def test_sync_is_bounded_and_acknowledges_only_after_successful_repository_push():
    text = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    assert "prediction-lab-data" in text
    assert 'MAX_FILES: "500"' in text and 'MAX_BYTES: "26214400"' in text
    assert 'cron: "*/5 * * * *"' in text
    assert "git pull --rebase" in text and "git push origin HEAD:prediction-lab-data" in text
    acknowledgement = "/admin/api/prediction-lab/sync-ack"
    assert acknowledgement in text
    assert text.index(acknowledgement) > text.index("git push origin HEAD:prediction-lab-data")
    assert "steps.push.outputs.ok == 'true'" in text
    assert "acknowledgement count mismatch" in text
    assert "actions: write" in text


def test_sync_uses_authenticated_http_bridge_not_railway_sftp():
    text = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    assert "/admin/api/prediction-lab/sync-status" in text
    assert "/admin/api/prediction-lab/sync-batch" in text
    assert "/admin/api/prediction-lab/sync-ack" in text
    assert 'RAILWAY_TOKEN: ${{ secrets.RAILWAY_API_TOKEN }}' in text
    assert "railway variable list" in text
    assert "ssh-keygen" not in text
    assert "railway service files" not in text
    assert "railway volume" not in text


def test_sync_self_drains_only_after_a_large_acknowledged_batch():
    text = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    assert 'continue_drain={"true" if removed >= 250 else "false"}' in text
    condition = "steps.acknowledge.outputs.continue_drain == 'true'"
    dispatch = "actions/workflows/prediction-lab-sync.yml/dispatches"
    assert condition in text and dispatch in text
    assert text.index(dispatch) > text.index("acknowledged_and_removed")


def test_sync_bridge_is_deliberately_raw_only_for_incident_recovery():
    workflow = (ROOT / ".github" / "workflows" / "prediction-lab-sync.yml").read_text(encoding="utf-8")
    bridge = (ROOT / "app" / "prediction_lab_sync_bridge_v5610.py").read_text(encoding="utf-8")
    assert "not relative.startswith('raw/')" in workflow
    assert "not rel.startswith(\"raw/\")" in bridge
    assert "archive/mongo-import" not in bridge


def test_pipeline_collect_is_idempotent(tmp_path: Path):
    raw = tmp_path / "prediction_lab" / "raw" / "2026-09-24"; raw.mkdir(parents=True)
    event = {"schema": "plane-alerts-prediction-evidence-v1", "case_id": "case-abc", "event_id": "evt-abc"}
    (raw / "evt-abc.json").write_text(json.dumps(event) + "\n", encoding="utf-8")
    script = ROOT / "scripts" / "prediction_lab_pipeline.py"
    cmd = [sys.executable, str(script), "--root", str(tmp_path / "prediction_lab"), "collect", "--limit", "10"]
    subprocess.run(cmd, check=True); subprocess.run(cmd, check=True)
    cases = list((tmp_path / "prediction_lab" / "unchecked").rglob("*.json"))
    assert len(cases) == 1
