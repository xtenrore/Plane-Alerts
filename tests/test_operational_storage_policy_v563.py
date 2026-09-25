from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_notification_volume_persists_and_expires_on_volume(tmp_path, monkeypatch):
    from app import notification_volume_v563 as volume
    from app.worker import notification_telemetry as telemetry

    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    monkeypatch.delenv("NOTIFICATION_TELEMETRY_DB_PATH", raising=False)
    volume._db = None
    volume._ready = False
    volume._ready_lock = None

    occurred = datetime(2026, 9, 25, 6, 0, tzinfo=timezone.utc)
    await telemetry._persist_event({
        "notification_id": "vol-1",
        "user_id": 7,
        "aircraft_icao24": "4baa10",
        "aircraft_type": "A321",
        "distance_km": 8.0,
        "projected_closest_km": 3.0,
        "observed_closest_km": None,
        "trajectory_state": "Approaching",
        "prediction_confidence": "High",
        "stage": "prepare",
        "input_message_id": None,
        "output_message_id": 99,
        "delivered": True,
        "delivery_detail": "sent",
        "occurred_at": occurred,
    })
    doc = await volume.notification_history_collection().find_one({"_id": "vol-1"})
    assert doc is not None
    assert doc["first_notified_at"] == occurred
    assert doc["expires_at"] > occurred
    assert volume.database_path() == tmp_path / "runtime" / "notification_history.sqlite3"
    assert volume.database_path().exists()
    await volume.close()


@pytest.mark.asyncio
async def test_legacy_notification_migration_copies_then_drops_only_operational_collections(tmp_path, monkeypatch):
    from app import notification_volume_v563 as volume

    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    volume._db = None
    volume._ready = False
    volume._ready_lock = None

    touched: list[tuple[str, str]] = []
    legacy_doc = {
        "_id": "old-1",
        "user_id": 1,
        "last_event_at": datetime(2026, 9, 25, 5, 0, tzinfo=timezone.utc),
        "events": [],
    }

    class Cursor:
        def batch_size(self, _n):
            return self
        def __aiter__(self):
            self.done = False
            return self
        async def __anext__(self):
            if self.done:
                raise StopAsyncIteration
            self.done = True
            return legacy_doc

    class Collection:
        def __init__(self, name):
            self.name = name
        async def count_documents(self, _query):
            return 1 if self.name == "notification_history" else 0
        def find(self, _query):
            assert self.name == "notification_history"
            return Cursor()
        async def drop(self):
            touched.append(("drop", self.name))

    class Mongo:
        def __getitem__(self, name):
            assert name in {"notification_history", "flight_route_samples"}
            return Collection(name)

    monkeypatch.setattr("app.database.get_db", lambda: Mongo())
    state = await volume.retire_legacy_mongo()

    copied = await volume.notification_history_collection().find_one({"_id": "old-1"})
    assert copied is not None
    assert state["source_count"] == 1
    assert state["imported"] == 1
    assert state["normal_application_data_untouched"] is True
    assert touched == [("drop", "notification_history"), ("drop", "flight_route_samples")]
    await volume.close()


@pytest.mark.asyncio
async def test_migration_count_mismatch_never_drops_legacy_collection(tmp_path, monkeypatch):
    from app import notification_volume_v563 as volume

    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(tmp_path))
    volume._db = None
    volume._ready = False
    volume._ready_lock = None
    dropped: list[str] = []

    class EmptyCursor:
        def batch_size(self, _n): return self
        def __aiter__(self): return self
        async def __anext__(self): raise StopAsyncIteration

    class Collection:
        def __init__(self, name): self.name = name
        async def count_documents(self, _query): return 2
        def find(self, _query): return EmptyCursor()
        async def drop(self): dropped.append(self.name)

    class Mongo:
        def __getitem__(self, name): return Collection(name)

    monkeypatch.setattr("app.database.get_db", lambda: Mongo())
    with pytest.raises(RuntimeError, match="count mismatch"):
        await volume.retire_legacy_mongo()
    assert dropped == []
    await volume.close()


def test_policy_forbids_all_known_high_volume_operational_mongo_collections():
    from app.operational_storage_policy_v563 import FORBIDDEN_OPERATIONAL_MONGO_COLLECTIONS

    assert {
        "flight_route_samples",
        "notification_history",
        "prediction_lab_audit",
        "prediction_shadow_evaluations",
        "prediction_sentinel_routes",
    } <= FORBIDDEN_OPERATIONAL_MONGO_COLLECTIONS


@pytest.mark.asyncio
async def test_mongo_index_policy_skips_operational_collections_but_keeps_user_indexes(monkeypatch):
    from app import operational_storage_policy_v563 as policy

    indexed: list[str] = []

    class Collection:
        def __init__(self, name): self.name = name
        async def create_index(self, *args, **kwargs):
            indexed.append(self.name)
            return self.name

    class Db:
        def __getitem__(self, name): return Collection(name)

    async def fake_original(db):
        await db["users"].create_index("user_id")
        await db["notification_history"].create_index("user_id")
        await db["flight_route_samples"].create_index("utc_date")
        await db["locations"].create_index("user_id")

    monkeypatch.setattr(policy, "_original_ensure_indexes", fake_original)
    await policy._ensure_indexes_without_operational_mongo(Db())
    assert indexed == ["users", "locations"]


def test_retired_operational_collections_are_not_used_by_active_notification_writer():
    source = Path("app/worker/notification_telemetry.py").read_text(encoding="utf-8")
    assert "get_db" not in source
    assert '"notification_history"' not in source
