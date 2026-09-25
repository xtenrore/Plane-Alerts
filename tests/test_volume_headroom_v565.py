from __future__ import annotations

import inspect
import json
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


def test_unverified_legacy_mongo_export_can_be_removed_without_touching_runtime_or_raw(tmp_path: Path):
    from app.volume_headroom_v565 import purge_unverified_legacy_export

    root = tmp_path / "lab"
    archive = root / "archive" / "mongo-import" / "prediction_lab_audit"
    raw = root / "raw" / "2026-09-25"
    runtime = root / "runtime"
    state = root / "state"
    archive.mkdir(parents=True)
    raw.mkdir(parents=True)
    runtime.mkdir(parents=True)
    state.mkdir(parents=True)

    (archive / "part-000000.ndjson").write_bytes(b"legacy-operational-export" * 20)
    (raw / "unsynced.json").write_bytes(b"unsynced-evidence")
    (runtime / "route_history.sqlite3").write_bytes(b"route-db")
    (runtime / "notification_history.sqlite3").write_bytes(b"notification-db")
    (state / "keep.json").write_bytes(b"state")

    result = purge_unverified_legacy_export(root=root)

    assert result["removed"] is True
    assert result["bytes"] > 0
    assert list((root / "archive" / "mongo-import").iterdir()) == []
    assert (raw / "unsynced.json").read_bytes() == b"unsynced-evidence"
    assert (runtime / "route_history.sqlite3").read_bytes() == b"route-db"
    assert (runtime / "notification_history.sqlite3").read_bytes() == b"notification-db"
    assert (state / "keep.json").read_bytes() == b"state"


def test_verified_legacy_archive_is_never_removed(tmp_path: Path):
    from app.volume_headroom_v565 import purge_unverified_legacy_export

    root = tmp_path / "lab"
    archive = root / "archive" / "mongo-import"
    archive.mkdir(parents=True)
    manifest = archive / "manifest.json"
    payload = archive / "prediction_lab_audit" / "part-000000.ndjson"
    payload.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"verified": True}), encoding="utf-8")
    payload.write_bytes(b"verified-archive")

    result = purge_unverified_legacy_export(root=root)

    assert result["removed"] is False
    assert result["reason"] == "verified_archive_protected"
    assert payload.read_bytes() == b"verified-archive"
    assert manifest.exists()


def test_volume_inventory_reports_runtime_and_archive_sizes(tmp_path: Path):
    from app.volume_headroom_v565 import volume_inventory

    root = tmp_path / "lab"
    (root / "runtime").mkdir(parents=True)
    (root / "archive" / "mongo-import").mkdir(parents=True)
    (root / "runtime" / "route_history.sqlite3").write_bytes(b"r" * 100)
    (root / "archive" / "mongo-import" / "part.ndjson").write_bytes(b"a" * 200)

    inventory = volume_inventory(root=root)
    assert inventory["categories"]["runtime"]["bytes"] >= 100
    assert inventory["categories"]["archive"]["bytes"] >= 200
    assert any(item["path"].endswith("part.ndjson") for item in inventory["largest_files"])


def test_sentinel_runs_critical_recovery_before_initial_delay_and_rechecks_frequently():
    import app.sentinel_network as sentinel

    source = inspect.getsource(sentinel.run_sentinel_network)
    assert source.index("await _recover_volume_startup()") < source.index("await asyncio.sleep(12.0)")
    assert sentinel.VOLUME_HEADROOM_CHECK_S <= 300.0
    assert "recover_critical_headroom" in inspect.getsource(sentinel._recover_volume_startup)
    assert "prune_acknowledged_evidence" in inspect.getsource(sentinel._maintain_volume_headroom)
