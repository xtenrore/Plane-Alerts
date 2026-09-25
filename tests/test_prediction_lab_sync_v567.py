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
    assert 'TRANSFER_WORKERS: "12"' in text


def test_v567_sync_rebuilds_volume_relative_paths_from_listed_directory_and_name() -> None:
    text = _text()
    assert "files=data.get('files')" in text
    assert "path=str(base / name)" in text
    assert "value.get('remotePath')" not in text
    assert "path=value.get('path')" not in text
    assert "malformed Railway volume listing" in text


def test_v567_sync_validates_bundled_raw_evidence() -> None:
    text = _text()
    assert "path.endswith('.ndjson')" in text
    assert "unsupported evidence schema in bundle" in text
    assert "json.loads(line)" in text


def test_v568_repository_acknowledged_removal_happens_only_after_successful_push() -> None:
    text = _text()
    commit_pos = text.index("- name: Commit and push evidence")
    push_pos = text.index("git push origin HEAD:prediction-lab-data", commit_pos)
    ack_pos = text.index("- name: Remove repository-acknowledged spool files")
    delete_pos = text.index("'delete',old,'--yes','--json'", ack_pos)
    assert commit_pos < push_pos < ack_pos < delete_pos
    assert "steps.push.outputs.ok == 'true'" in text
    assert "new=old+'.synced'" not in text


def test_v568_sync_bootstraps_on_main_workflow_change_and_self_drains_large_backlog() -> None:
    text = _text()
    assert "branches: [main]" in text
    assert '- ".github/workflows/prediction-lab-sync.yml"' in text
    assert "actions: write" in text
    assert "cancel-in-progress: false" in text
    ack_pos = text.index("- name: Remove repository-acknowledged spool files")
    continuation_pos = text.index("- name: Continue draining a saturated backlog")
    dispatch_pos = text.index("actions/workflows/prediction-lab-sync.yml/dispatches", continuation_pos)
    assert ack_pos < continuation_pos < dispatch_pos
    assert "steps.acknowledge.outputs.continue_drain == 'true'" in text
    assert "-f ref=main" in text
