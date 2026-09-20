from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.storage_backup_v48 import export_storage, import_storage
from app.storage_metrics_v48 import StorageMetrics, storage_metrics
from app.storage_migrations_v48 import run_migrations
from app.storage_runtime_v48 import StorageRuntimeV48
import app.storage_runtime_v48 as storage_runtime_module


class AsyncCursor:
    def __init__(self, rows):
        self.rows = [deepcopy(row) for row in rows]
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return deepcopy(row)


class FakeCollection:
    def __init__(self, rows=None):
        self.rows = [deepcopy(row) for row in (rows or [])]
        self.fail_reads = False
        self.fail_writes = False
        self.write_delay = 0.0
        self.indexes = []

    def _matches(self, row, query):
        for key, expected in (query or {}).items():
            if key == "$or":
                if not any(self._matches(row, item) for item in expected):
                    return False
                continue
            if isinstance(expected, dict):
                if "$in" in expected and row.get(key) not in expected["$in"]:
                    return False
                if "$gt" in expected:
                    value = row.get(key)
                    if value is None or value <= expected["$gt"]:
                        return False
                if "$lte" in expected:
                    value = row.get(key)
                    if value is None or value > expected["$lte"]:
                        return False
                if "$exists" in expected:
                    if (key in row) != bool(expected["$exists"]):
                        return False
                continue
            if row.get(key) != expected:
                return False
        return True

    def find(self, query=None, projection=None):
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        return AsyncCursor([row for row in self.rows if self._matches(row, query or {})])

    async def find_one(self, query, projection=None):
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        for row in self.rows:
            if self._matches(row, query):
                return deepcopy(row)
        return None

    async def create_index(self, *args, **kwargs):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        self.indexes.append((args, kwargs))
        return "idx"

    async def insert_one(self, document):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.rows.append(deepcopy(document))
        return SimpleNamespace(inserted_id=document.get("_id"))

    async def update_one(self, query, update, upsert=False):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        for row in self.rows:
            if self._matches(row, query):
                row.update(deepcopy(update.get("$set") or {}))
                for key, value in (update.get("$setOnInsert") or {}).items():
                    row.setdefault(key, deepcopy(value))
                return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if upsert:
            document = {key: deepcopy(value) for key, value in query.items() if not key.startswith("$") and not isinstance(value, dict)}
            document.update(deepcopy(update.get("$set") or {}))
            document.update(deepcopy(update.get("$setOnInsert") or {}))
            self.rows.append(document)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id="new")
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

    async def replace_one(self, query, document, upsert=False):
        if self.fail_writes:
            raise RuntimeError("database unavailable")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        for index, row in enumerate(self.rows):
            if self._matches(row, query):
                self.rows[index] = deepcopy(document)
                return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if upsert:
            self.rows.append(deepcopy(document))
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id="new")
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)


class FakeDB:
    def __init__(self, collections=None):
        self.collections = dict(collections or {})
        self.ping_fail = False

    def __getitem__(self, name):
        self.collections.setdefault(name, FakeCollection())
        return self.collections[name]

    async def command(self, name):
        if self.ping_fail:
            raise RuntimeError("database unavailable")
        return {"ok": 1}


def configured_db():
    now = datetime.now(timezone.utc)
    return FakeDB(
        {
            "users": FakeCollection([{"user_id": 7, "setup_complete": True, "admin_controls": {"priority_enabled": True}}]),
            "locations": FakeCollection([{"user_id": 7, "latitude": 41.0, "longitude": 29.0, "radius_km": 15, "config_revision": "r1"}]),
            "preferences": FakeCollection([{"user_id": 7, "config_revision": "r1", "aircraft_filter": {"mode": "all"}}]),
            "approach_states": FakeCollection([
                {
                    "user_id": 7,
                    "aircraft_icao24": "abc123",
                    "active": True,
                    "first_notification_sent": True,
                    "updated_at": now,
                    "expires_at": now + timedelta(minutes=20),
                }
            ]),
        }
    )


@pytest.mark.asyncio
async def test_warm_cache_survives_database_disappearance():
    db = configured_db()
    runtime = StorageRuntimeV48()
    await runtime.warm(db)
    before = runtime.active_users()
    assert before and before[0]["user_id"] == 7

    db["users"].fail_reads = True
    assert await runtime.refresh_config_once(db) is False
    assert runtime.active_users() == before
    assert runtime.monitoring_safe is True


@pytest.mark.asyncio
async def test_restart_warm_restores_delivered_alert_state_without_duplicate_reset():
    runtime = StorageRuntimeV48()
    await runtime.warm(configured_db())
    restored = runtime.approach_states(7, {"abc123"})["abc123"]
    assert restored["active"] is True
    assert restored["first_notification_sent"] is True


@pytest.mark.asyncio
async def test_lifecycle_update_is_memory_first_and_coalesced():
    runtime = StorageRuntimeV48()
    await runtime.warm(configured_db())
    cycle_cache = runtime.approach_states(7, {"abc123"})
    updated_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    result = runtime.apply_approach_update(
        7,
        cycle_cache,
        {"aircraft_icao24": "abc123"},
        {"$set": {"active": False, "cancellation_delivered": True, "updated_at": updated_at}},
    )

    assert result.modified_count == 1
    assert cycle_cache["abc123"]["cancellation_delivered"] is True
    assert runtime.approach_states(7, {"abc123"})["abc123"]["active"] is False
    assert runtime.snapshot()["critical_write_queue_depth"] == 1


@pytest.mark.asyncio
async def test_slow_write_does_not_block_hot_memory_reads():
    db = configured_db()
    db["approach_states"].write_delay = 0.25
    runtime = StorageRuntimeV48()
    await runtime.warm(db)
    cache = runtime.approach_states(7, {"abc123"})
    runtime.apply_approach_update(
        7,
        cache,
        {"aircraft_icao24": "abc123"},
        {"$set": {"updated_at": datetime.now(timezone.utc) + timedelta(seconds=1)}},
    )

    flush = asyncio.create_task(runtime.flush_once(db))
    await asyncio.sleep(0.02)
    for _ in range(200):
        assert runtime.active_users()[0]["user_id"] == 7
        assert "abc123" in runtime.approach_states(7, {"abc123"})
    await flush


@pytest.mark.asyncio
async def test_failed_flush_uses_bounded_retry_backoff():
    db = configured_db()
    runtime = StorageRuntimeV48()
    await runtime.warm(db)
    cache = runtime.approach_states(7, {"abc123"})
    runtime.apply_approach_update(
        7,
        cache,
        {"aircraft_icao24": "abc123"},
        {"$set": {"updated_at": datetime.now(timezone.utc) + timedelta(seconds=1)}},
    )
    db["approach_states"].fail_writes = True

    assert await runtime.flush_once(db) == 0
    first = runtime.snapshot()
    assert first["flush_failures"] == 1
    assert first["flush_retry_in_s"] > 0
    # Immediate retry is suppressed rather than hammering the failed database.
    assert await runtime.flush_once(db) == 0
    assert runtime.snapshot()["flush_failures"] == 1


def test_optional_queue_is_bounded_and_drops_oldest(monkeypatch):
    monkeypatch.setattr(storage_runtime_module, "_OPTIONAL_PENDING_MAX", 2)
    runtime = StorageRuntimeV48()
    before = storage_metrics.snapshot()["optional_writes_dropped"]
    runtime.enqueue_optional_document("analytics", {"value": 1}, document_id="one")
    runtime.enqueue_optional_document("analytics", {"value": 2}, document_id="two")
    runtime.enqueue_optional_document("analytics", {"value": 3}, document_id="three")
    assert runtime.snapshot()["optional_write_queue_depth"] == 2
    assert ("analytics", "one") not in runtime._optional_pending
    assert storage_metrics.snapshot()["optional_writes_dropped"] >= before + 1


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_versioned():
    db = FakeDB()
    assert await run_migrations(db) == 1
    first = deepcopy(db["schema_migrations"].rows)
    assert len(first) == 1
    assert first[0]["migration_id"] == "v48-storage-baseline"
    assert await run_migrations(db) == 1
    assert len(db["schema_migrations"].rows) == 1


@pytest.mark.asyncio
async def test_migration_rejects_version_id_mismatch():
    db = FakeDB({"schema_migrations": FakeCollection([{"version": 1, "migration_id": "wrong-id"}])})
    with pytest.raises(RuntimeError, match="id mismatch"):
        await run_migrations(db)


@pytest.mark.asyncio
async def test_export_is_secret_free_and_location_safe_by_default():
    db = FakeDB(
        {
            "users": FakeCollection([{"user_id": 1, "telegram_bot_token": "secret", "updated_at": datetime.now(timezone.utc)}]),
            "preferences": FakeCollection([{"user_id": 1, "api_key": "secret", "mode": "all"}]),
            "locations": FakeCollection([{"user_id": 1, "latitude": 41.0, "longitude": 29.0}]),
        }
    )
    payload = await export_storage(db)
    assert payload["includes_exact_locations"] is False
    assert "locations" not in payload["collections"]
    serialized = repr(payload)
    assert "telegram_bot_token" not in serialized
    assert "api_key" not in serialized
    assert "secret" not in serialized


@pytest.mark.asyncio
async def test_import_rejects_malformed_and_never_overwrites_newer_record():
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(days=1)
    db = FakeDB({"users": FakeCollection([{"user_id": 1, "updated_at": newer, "setup_complete": True}])})
    payload = {
        "format": "plane-alerts-storage-export",
        "schema": 1,
        "includes_exact_locations": False,
        "collections": {
            "users": [
                {
                    "user_id": 1,
                    "updated_at": {"$plane_datetime": older.isoformat()},
                    "setup_complete": False,
                }
            ],
            "unknown_collection": [{"id": 1}],
        },
    }
    report = await import_storage(db, payload)
    assert report["skipped"] == 1
    assert report["rejected"] == 1
    assert db["users"].rows[0]["setup_complete"] is True

    with pytest.raises(ValueError):
        await import_storage(db, {"format": "wrong", "schema": 1})


def test_storage_metrics_reports_latency_distribution_and_recovery():
    metrics = StorageMetrics(max_samples=8)
    for value in (100.0, 500.0, 1000.0):
        metrics.record_command("find", value, True)
    metrics.record_command("update", 250.0, False, timeout=True)
    assert metrics.snapshot()["database_state"] == "degraded"
    metrics.record_command("ping", 25.0, True)
    snap = metrics.snapshot()
    assert snap["database_state"] == "healthy"
    assert snap["read_latency"]["p99_ms"] == 1000.0
    assert snap["write_latency"]["max_ms"] == 250.0
    assert snap["timeouts"] == 1
