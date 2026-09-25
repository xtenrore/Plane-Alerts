from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(".github/workflows/prediction-lab-sync.yml")


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_v567_sync_runs_frequently_with_bounded_higher_throughput() -> None:
    text = _text()
    assert 'cron: "*/5 * * * *"' in text
    assert 'MAX_FILES: "500"' in text
    assert 'MAX_BYTES: "26214400"' in text
    assert 'timeout-minutes: 20' in text


def test_v5610_uses_existing_project_token_only_for_variable_lookup() -> None:
    text = _text()
    assert 'RAILWAY_TOKEN: ${{ secrets.RAILWAY_API_TOKEN }}' in text
    assert 'RAILWAY_API_TOKEN:' not in text
    assert 'railway variable list --project "$PROJECT_ID" --environment "$ENVIRONMENT_ID" --service "$MAIN_SERVICE_ID" --json' in text
    assert "ADMIN_PASSWORD" in text
    assert 'echo "::add-mask::$ADMIN_PASSWORD"' in text
    assert "ssh-keygen" not in text
    assert "railway service files" not in text
    assert "railway volume" not in text


def test_v5610_waits_for_exact_deployed_bridge_before_export() -> None:
    text = _text()
    assert 'REQUIRED_BRIDGE_VERSION: "5.6.10"' in text
    wait_pos = text.index("- name: Wait for exact deployed sync bridge")
    download_pos = text.index("- name: Download bounded authenticated evidence batch")
    assert wait_pos < download_pos
    assert "/admin/api/prediction-lab/sync-status" in text[wait_pos:download_pos]
    assert 'FOUND_VERSION" == "$REQUIRED_BRIDGE_VERSION' in text[wait_pos:download_pos]


def test_v5610_independently_validates_zip_hash_schema_and_paths() -> None:
    text = _text()
    assert "duplicate ZIP member rejected" in text
    assert "plane-alerts-prediction-sync-batch-v1" in text
    assert "unexpected Prediction Lab sync bridge version" in text
    assert "not relative.startswith('raw/')" in text
    assert "Prediction Lab content hash mismatch" in text
    assert "credential-like material rejected" in text
    assert "unsupported evidence schema in bundle" in text
    assert "unexpected ZIP members rejected" in text


def test_v5610_repository_acknowledgement_happens_only_after_successful_push() -> None:
    text = _text()
    commit_pos = text.index("- name: Commit and push evidence")
    push_pos = text.index("git push origin HEAD:prediction-lab-data", commit_pos)
    ack_pos = text.index("- name: Acknowledge only repository-verified evidence")
    post_pos = text.index("/admin/api/prediction-lab/sync-ack", ack_pos)
    assert commit_pos < push_pos < ack_pos < post_pos
    assert "steps.push.outputs.ok == 'true'" in text
    assert "acknowledgement count mismatch" in text


def test_v568_sync_bootstraps_on_bridge_changes_and_self_drains_large_backlog() -> None:
    text = _text()
    assert "branches: [main]" in text
    assert '- ".github/workflows/prediction-lab-sync.yml"' in text
    assert '- "app/prediction_lab_sync_bridge_v5610.py"' in text
    assert '- "app/admin/prediction_lab_sync_v5610.py"' in text
    assert "actions: write" in text
    assert "cancel-in-progress: false" in text
    ack_pos = text.index("- name: Acknowledge only repository-verified evidence")
    continuation_pos = text.index("- name: Continue draining a saturated backlog")
    dispatch_pos = text.index("actions/workflows/prediction-lab-sync.yml/dispatches", continuation_pos)
    assert ack_pos < continuation_pos < dispatch_pos
    assert "steps.acknowledge.outputs.continue_drain == 'true'" in text
    assert "-f ref=main" in text
