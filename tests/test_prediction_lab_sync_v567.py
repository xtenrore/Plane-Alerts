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


def test_v569_sync_targets_live_service_filesystem_at_exact_mount() -> None:
    text = _text()
    assert "PREDICTION_LAB_MOUNT: /data/prediction_lab" in text
    assert "['railway','service','files','--project',project,'--environment',environment,'--service',service,*args]" in text
    assert "collect_tree(service_root + '/raw')" in text
    assert "collect_tree(service_root + '/archive/mongo-import')" in text
    assert "remote.startswith(prefix)" in text
    assert "remote evidence escaped Prediction Lab mount" in text
    assert "Railway service-files command failed" in text


def test_v569_sync_rebuilds_absolute_service_paths_from_listed_directory_and_name() -> None:
    text = _text()
    assert "files=data.get('files')" in text
    assert "path=str(base / name)" in text
    assert "value.get('remotePath')" not in text
    assert "path=value.get('path')" not in text
    assert "malformed Railway service-files listing" in text


def test_v5610_uses_ephemeral_workspace_ssh_and_read_only_pr_preflight() -> None:
    text = _text()
    assert "pull_request:" in text
    assert "RAILWAY_API_TOKEN: ${{ secrets.RAILWAY_API_TOKEN }}" in text
    assert "RAILWAY_TOKEN: ${{ secrets.RAILWAY_TOKEN }}" not in text
    assert "RAILWAY_TOKEN must remain unset" in text
    assert 'ssh-keygen -q -t ed25519 -N ""' in text
    add_pos = text.index('railway ssh keys add --key "$KEY_PATH" --name "$KEY_NAME"')
    preflight_pos = text.index("- name: Verify Railway SSH file transport")
    assert add_pos < preflight_pos
    assert 'list "$PREDICTION_LAB_MOUNT/raw" --json' in text
    download_pos = text.index("- name: Download bounded evidence batch")
    assert "github.event_name != 'pull_request'" in text[download_pos: text.index("- name: Validate and stage evidence")]


def test_v5610_always_removes_exact_ephemeral_railway_ssh_key() -> None:
    text = _text()
    cleanup_pos = text.index("- name: Remove ephemeral Railway SSH key")
    assert "if: always() && steps.ssh.outputs.registered == 'true'" in text[cleanup_pos:]
    assert 'RAILWAY_SSH_KEY_NAME: ${{ steps.ssh.outputs.name }}' in text[cleanup_pos:]
    assert 'railway ssh keys remove "$RAILWAY_SSH_KEY_NAME"' in text[cleanup_pos:]
    assert 'rm -f "$HOME/.ssh/id_ed25519" "$HOME/.ssh/id_ed25519.pub"' in text[cleanup_pos:]


def test_v567_sync_validates_bundled_raw_evidence() -> None:
    text = _text()
    assert "path.endswith('.ndjson')" in text
    assert "unsupported evidence schema in bundle" in text
    assert "json.loads(line)" in text


def test_v569_repository_acknowledged_removal_happens_only_after_successful_push() -> None:
    text = _text()
    commit_pos = text.index("- name: Commit and push evidence")
    push_pos = text.index("git push origin HEAD:prediction-lab-data", commit_pos)
    ack_pos = text.index("- name: Remove repository-acknowledged spool files")
    delete_pos = text.index("cmd('delete',old,'--yes','--json')", ack_pos)
    cleanup_pos = text.index("- name: Remove ephemeral Railway SSH key")
    assert commit_pos < push_pos < ack_pos < delete_pos < cleanup_pos
    assert "steps.push.outputs.ok == 'true'" in text
    assert "old.startswith(service_root + '/raw/')" in text
    assert "old.startswith(service_root + '/archive/mongo-import/')" in text
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
