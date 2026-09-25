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


def test_v567_acknowledgement_still_happens_only_after_successful_repository_push() -> None:
    text = _text()
    commit_pos = text.index("- name: Commit and push evidence")
    push_pos = text.index("git push origin HEAD:prediction-lab-data", commit_pos)
    ack_pos = text.index("- name: Acknowledge committed spool files")
    rename_pos = text.index("'rename',old,new,'--json'", ack_pos)
    assert commit_pos < push_pos < ack_pos < rename_pos
    assert "steps.push.outputs.ok == 'true'" in text
    assert "files','--volume',volume,'delete'" not in text
