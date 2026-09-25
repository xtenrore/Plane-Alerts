from __future__ import annotations

import inspect
from pathlib import Path


def test_headroom_guard_deletes_only_acknowledged_synced_evidence(tmp_path: Path):
    from app.volume_headroom_v565 import prune_acknowledged_evidence

    root = tmp_path / "lab"
    raw = root / "raw" / "2026-09-25"
    archive = root / "archive" / "mongo-import" / "prediction_lab_audit"
    runtime = root / "runtime"
    raw.mkdir(parents=True)
    archive.mkdir(parents=True)
    runtime.mkdir(parents=True)

    synced_raw = raw / "evt-a.json.synced"
    synced_archive = archive / "part-000001.ndjson.synced"
    unsynced = raw / "evt-b.json"
    route_db = runtime / "route_history.sqlite3"
    notification_db = runtime / "notification_history.sqlite3"

    synced_raw.write_bytes(b"a" * 128)
    synced_archive.write_bytes(b"b" * 256)
    unsynced.write_bytes(b"important-unsynced-evidence")
    route_db.write_bytes(b"route-sqlite")
    notification_db.write_bytes(b"notification-sqlite")

    result = prune_acknowledged_evidence(
        root=root,
        target_free_bytes=10**18,
        max_files=100,
    )

    assert result["removed_files"] == 2
    assert result["removed_bytes"] == 384
    assert not synced_raw.exists()
    assert not synced_archive.exists()
    assert unsynced.read_bytes() == b"important-unsynced-evidence"
    assert route_db.read_bytes() == b"route-sqlite"
    assert notification_db.read_bytes() == b"notification-sqlite"


def test_headroom_guard_is_bounded_by_max_files(tmp_path: Path):
    from app.volume_headroom_v565 import prune_acknowledged_evidence

    root = tmp_path / "lab"
    raw = root / "raw" / "2026-09-25"
    raw.mkdir(parents=True)
    for index in range(5):
        (raw / f"evt-{index}.json.synced").write_bytes(b"x" * 16)

    result = prune_acknowledged_evidence(
        root=root,
        target_free_bytes=10**18,
        max_files=2,
    )

    assert result["removed_files"] == 2
    assert len(list(raw.glob("*.synced"))) == 3


def test_sentinel_recovers_headroom_before_initial_delay_and_rechecks_frequently():
    import app.sentinel_network as sentinel

    source = inspect.getsource(sentinel.run_sentinel_network)
    assert source.index("await _maintain_volume_headroom()") < source.index("await asyncio.sleep(12.0)")
    assert sentinel.VOLUME_HEADROOM_CHECK_S <= 300.0
    assert "prune_acknowledged_evidence" in inspect.getsource(sentinel._maintain_volume_headroom)
