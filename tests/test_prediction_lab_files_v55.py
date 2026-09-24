from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import prediction_lab_files_v55 as files


class AsyncCursor:
    def __init__(self, rows): self.rows = list(rows)
    def sort(self, *args, **kwargs): return self
    def batch_size(self, *args, **kwargs): return self
    def __aiter__(self): self._it = iter(self.rows); return self
    async def __anext__(self):
        try: return next(self._it)
        except StopIteration: raise StopAsyncIteration


class FakeCollection:
    def __init__(self, rows): self.rows = list(rows); self.dropped = False
    async def count_documents(self, query): assert query == {}; return len(self.rows)
    def find(self, query): assert query == {}; return AsyncCursor(self.rows)
    async def drop(self): self.dropped = True


class FakeDB:
    def __init__(self, mapping): self.mapping = mapping
    def __getitem__(self, name): return self.mapping[name]


def test_atomic_spool_is_deduplicated_private_and_inconclusive(tmp_path: Path):
    root = tmp_path / "lab"
    doc = {"kind": "prediction", "captured_at": datetime(2026, 9, 24, 1, 2, tzinfo=timezone.utc), "user_id": 123456789, "aircraft_icao24": "4bb123", "coverage_missing": True, "authorization": "Bearer must-not-leak", "nested": {"api_key": "must-not-leak-either"}}
    first = files.write_evidence(doc, root=root); second = files.write_evidence(doc, root=root)
    assert first == second
    payload = json.loads(first.read_text(encoding="utf-8")); text = first.read_text(encoding="utf-8")
    assert payload["coverage_resolution"] == "inconclusive"
    assert payload["observer_ref"].startswith("observer-")
    assert "123456789" not in text and "must-not-leak" not in text
    assert payload["case_id"].startswith("case-") and payload["event_id"].startswith("evt-")
    assert not list(first.parent.glob("*.tmp"))


def test_historical_expectation_key_is_privacy_safe(tmp_path: Path):
    clean = files.sanitize_for_repository({"expectation_key": "123456789:THY7:2026-09-24", "user_id": 123456789}, root=tmp_path)
    encoded = json.dumps(clean, sort_keys=True)
    assert "123456789" not in encoded
    assert clean["expectation_key"].endswith(":THY7:2026-09-24")
    assert clean["observer_ref"].startswith("observer-")


@pytest.mark.asyncio
async def test_mongo_migration_verifies_counts_hashes_then_drops_only_lab_collections(tmp_path: Path):
    rows = {name: [{"_id": f"{name}-1", "kind": "prediction", "user_id": 42, "prediction_version": "5.3-3d-proximity-age-aware", "captured_at": datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc), "outcome": "passed"}] for name in files.LAB_COLLECTIONS}
    collections = {name: FakeCollection(value) for name, value in rows.items()}; normal = FakeCollection([{"_id": "normal"}]); collections["users"] = normal
    manifest = await files.migrate_prediction_lab_mongo(FakeDB(collections), root=tmp_path / "lab")
    assert manifest["verified"] is True and manifest["legacy_collections_dropped"] is True and normal.dropped is False
    for name in files.LAB_COLLECTIONS:
        assert collections[name].dropped is True
        details = manifest["collections"][name]
        assert details["source_count"] == details["exported_count"] == 1 and details["verified"] is True
        for chunk in details["chunks"]:
            path = tmp_path / "lab" / chunk["path"]; data = path.read_bytes()
            assert hashlib.sha256(data).hexdigest() == chunk["sha256"] and data.count(b"\n") == chunk["records"]
            assert b'"prediction_version":"5.3-3d-proximity-age-aware"' in data and b'"outcome":"passed"' in data and b'"user_id"' not in data
    state = files.migration_state(tmp_path / "lab")
    assert state["verified"] is True and state["normal_application_data_untouched"] is True


@pytest.mark.asyncio
async def test_verified_migration_restart_does_not_reexport(tmp_path: Path):
    root = tmp_path / "lab"; files.ensure_layout(root)
    (root / "state" / "mongo_migration_v55.json").write_text(json.dumps({"verified": True, "legacy_collections_dropped": False, "collections": {}}), encoding="utf-8")
    collections = {name: FakeCollection([]) for name in files.LAB_COLLECTIONS}
    result = await files.migrate_prediction_lab_mongo(FakeDB(collections), root=root)
    assert result["verified"] is True and all(collection.dropped for collection in collections.values())
    assert not list((root / "archive" / "mongo-import").rglob("part-*.ndjson"))


def test_cleanup_only_removes_acknowledged_synced_files(tmp_path: Path):
    root = files.ensure_layout(tmp_path / "lab"); raw = root / "raw" / "2026-09-24"; raw.mkdir(parents=True)
    synced = raw / "evt-a.json.synced"; unsynced = raw / "evt-b.json"; synced.write_text("{}\n"); unsynced.write_text("{}\n")
    old = time.time() - 48 * 3600; os.utime(synced, (old, old)); os.utime(unsynced, (old, old))
    assert files.prune_synced(root=root, older_than_hours=24, max_files=10) == 1
    assert not synced.exists() and unsynced.exists()
