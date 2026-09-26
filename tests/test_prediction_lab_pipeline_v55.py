from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_repository_pipeline_structure_and_durable_checkpoints_exist():
    expected = (
        "prediction_lab/raw",
        "prediction_lab/unchecked",
        "prediction_lab/reviewed",
        "prediction_lab/solved",
        "prediction_lab/error_museum",
        "prediction_lab/archive/mongo-import",
        "prediction_lab/state",
        "prediction_lab/schemas",
    )
    for rel in expected:
        assert (ROOT / rel).exists(), rel
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
    raw = tmp_path / "prediction_lab" / "raw" / "2026-09-24"
    raw.mkdir(parents=True)
    event = {
        "schema": "plane-alerts-prediction-evidence-v1",
        "case_id": "case-abc",
        "event_id": "evt-abc",
    }
    (raw / "evt-abc.json").write_text(json.dumps(event) + "\n", encoding="utf-8")
    script = ROOT / "scripts" / "prediction_lab_pipeline.py"
    cmd = [sys.executable, str(script), "--root", str(tmp_path / "prediction_lab"), "collect", "--limit", "10"]
    subprocess.run(cmd, check=True)
    subprocess.run(cmd, check=True)
    cases = list((tmp_path / "prediction_lab" / "unchecked").rglob("*.json"))
    assert len(cases) == 1


def test_reviewed_and_solved_cases_are_preserved_and_not_recreated(tmp_path: Path):
    lab = tmp_path / "prediction_lab"
    raw = lab / "raw" / "2026-09-24"
    raw.mkdir(parents=True)
    event_path = raw / "evt-abc.json"
    event_path.write_text(
        json.dumps({
            "schema": "plane-alerts-prediction-evidence-v1",
            "case_id": "case-abc",
            "event_id": "evt-abc",
            "captured_at": "2026-09-24T12:00:00Z",
        }) + "\n",
        encoding="utf-8",
    )
    script = ROOT / "scripts" / "prediction_lab_pipeline.py"

    subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "collect",
        "--limit",
        "10",
    ], check=True)
    unchecked = next((lab / "unchecked").rglob("case-abc.json"))

    subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "review",
        str(unchecked),
        "--disposition",
        "confirmed_bug",
        "--evidence",
        "Exact replay reproduced the failure.",
    ], check=True)
    reviewed = next((lab / "reviewed").rglob("case-abc.json"))
    reviewed_payload = json.loads(reviewed.read_text(encoding="utf-8"))
    assert reviewed_payload["pipeline_stage"] == "reviewed"
    assert reviewed_payload["case_id"] == "case-abc"
    assert reviewed_payload["event_ids"] == ["evt-abc"]
    assert not list((lab / "unchecked").rglob("case-abc.json"))

    # Immutable raw evidence remains, but a collector rerun must not recreate
    # the already-reviewed historical case in unchecked.
    subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "collect",
        "--limit",
        "10",
    ], check=True)
    assert event_path.exists()
    assert not list((lab / "unchecked").rglob("case-abc.json"))
    assert len(list((lab / "reviewed").rglob("case-abc.json"))) == 1

    fix_sha = "a" * 40
    subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "solve",
        str(reviewed),
        "--fix-commit",
        fix_sha,
        "--deployed-version",
        "5.7.1",
        "--verification-evidence",
        "Exact SHA deployed; health, cadence and regression checks passed.",
        "--error-museum-ref",
        "prediction_lab/error_museum/case-abc.json",
    ], check=True)

    solved = next((lab / "solved").rglob("case-abc.json"))
    solved_payload = json.loads(solved.read_text(encoding="utf-8"))
    assert not reviewed.exists()
    assert event_path.exists()
    assert solved_payload["pipeline_stage"] == "solved"
    assert solved_payload["case_id"] == reviewed_payload["case_id"]
    assert solved_payload["event_ids"] == reviewed_payload["event_ids"]
    assert solved_payload["review"] == reviewed_payload["review"]
    assert solved_payload["resolution"]["status"] == "production_verified"
    assert solved_payload["resolution"]["fix_commit_sha"] == fix_sha
    assert solved_payload["resolution"]["deployed_version"] == "5.7.1"
    assert solved_payload["resolution"]["verification_evidence"]
    assert solved_payload["resolution"]["production_verified_at"]
    assert solved_payload["resolution"]["error_museum_refs"] == ["prediction_lab/error_museum/case-abc.json"]

    release_state = json.loads((lab / "state" / "release.json").read_text(encoding="utf-8"))
    assert release_state["last_case_id"] == "case-abc"
    assert release_state["last_event_id"] == "evt-abc"

    # A future collector pass still must not reopen the solved historical case.
    subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "collect",
        "--limit",
        "10",
    ], check=True)
    assert not list((lab / "unchecked").rglob("case-abc.json"))
    assert len(list((lab / "solved").rglob("case-abc.json"))) == 1


def test_solve_refuses_unreviewed_case(tmp_path: Path):
    lab = tmp_path / "prediction_lab"
    unchecked = lab / "unchecked" / "2026-09-24" / "case-unsafe.json"
    unchecked.parent.mkdir(parents=True)
    unchecked.write_text(
        json.dumps({
            "schema": "plane-alerts-case-v1",
            "pipeline_stage": "unchecked",
            "case_id": "case-unsafe",
            "event_ids": ["evt-unsafe"],
            "review": None,
        }) + "\n",
        encoding="utf-8",
    )
    script = ROOT / "scripts" / "prediction_lab_pipeline.py"
    result = subprocess.run([
        sys.executable,
        str(script),
        "--root",
        str(lab),
        "solve",
        str(unchecked),
        "--fix-commit",
        "b" * 40,
        "--deployed-version",
        "5.7.1",
        "--verification-evidence",
        "Should not be accepted.",
    ], check=False)
    assert result.returncode != 0
    assert unchecked.exists()
    assert not list((lab / "solved").rglob("case-unsafe.json"))
