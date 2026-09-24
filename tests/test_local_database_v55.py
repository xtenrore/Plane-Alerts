from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.local_database_v55 import SQLiteDatabase
from app.storage_migrations_v48 import EXPECTED_SCHEMA_VERSION, run_migrations
from app.storage_runtime_v48 import StorageRuntimeV48


@pytest.mark.asyncio
async def test_sqlite_persists_across_restart_and_preserves_datetimes(tmp_path: Path):
    path = tmp_path / "planealerts.db"
    now = datetime.now(timezone.utc)

    first = SQLiteDatabase(path)
    await first["users"].create_index("user_id", unique=True)
    await first["users"].insert_one(
        {"user_id": 42, "setup_complete": True, "created_at": now, "nested": {"enabled": True}}
    )
    await first.close()

    second = SQLiteDatabase(path)
    row = await second["users"].find_one({"user_id": 42})
    assert row is not None
    assert row["created_at"] == now
    assert row["nested"]["enabled"] is True
    ok, detail = await second.integrity_check()
    assert ok is True
    assert detail.lower() == "ok"
    await second.close()


@pytest.mark.asyncio
async def test_sqlite_supports_plane_alerts_query_update_and_projection_semantics(tmp_path: Path):
    db = SQLiteDatabase(tmp_path / "planealerts.db")
    users = db["users"]
    await users.create_index("user_id", unique=True)
    await users.insert_one({"user_id": 1, "setup_complete": True, "score": 1, "tags": ["a"]})
    await users.update_one(
        {"user_id": 1},
        {"$inc": {"score": 2}, "$set": {"admin_controls.priority_enabled": True}, "$addToSet": {"tags": "b"}},
    )
    await users.update_one(
        {"user_id": 2},
        {"$setOnInsert": {"created_at": datetime.now(timezone.utc)}, "$set": {"setup_complete": True}},
        upsert=True,
    )

    rows = [
        row
        async for row in users.find(
            {"$or": [{"user_id": {"$in": [1]}}, {"user_id": 2}]},
            {"user_id": 1, "score": 1, "_id": 0},
        ).sort("user_id", -1)
    ]
    assert [row["user_id"] for row in rows] == [2, 1]
    assert rows[1]["score"] == 3

    with pytest.raises(ValueError):
        await users.insert_one({"user_id": 1})
    await db.close()


@pytest.mark.asyncio
async def test_sqlite_ttl_index_removes_expired_documents(tmp_path: Path):
    db = SQLiteDatabase(tmp_path / "planealerts.db")
    states = db["approach_states"]
    await states.create_index("expires_at", expireAfterSeconds=0)
    await states.insert_one(
        {
            "user_id": 1,
            "aircraft_icao24": "abc123",
            "active": False,
            "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
        }
    )
    assert await states.count_documents({}) == 0
    await db.close()


@pytest.mark.asyncio
async def test_existing_plane_alerts_schema_migrations_run_on_sqlite(tmp_path: Path):
    db = SQLiteDatabase(tmp_path / "planealerts.db")
    version = await run_migrations(db)
    assert version == EXPECTED_SCHEMA_VERSION
    recorded = await db["schema_migrations"].find_one({"version": EXPECTED_SCHEMA_VERSION})
    assert recorded is not None
    assert recorded["migration_id"] == "v48-storage-baseline"
    await db.close()


@pytest.mark.asyncio
async def test_storage_runtime_can_warm_from_sqlite_without_changing_live_semantics(tmp_path: Path):
    db = SQLiteDatabase(tmp_path / "planealerts.db")
    revision = "r1"
    now = datetime.now(timezone.utc)
    await db["users"].insert_one({"user_id": 7, "setup_complete": True, "admin_controls": {}})
    await db["locations"].insert_one(
        {"user_id": 7, "latitude": 41.0, "longitude": 29.0, "config_revision": revision}
    )
    await db["preferences"].insert_one(
        {"user_id": 7, "radius_km": 15.0, "config_revision": revision}
    )
    await db["approach_states"].insert_one(
        {
            "user_id": 7,
            "aircraft_icao24": "4ba123",
            "active": True,
            "updated_at": now,
            "expires_at": now + timedelta(minutes=10),
        }
    )

    runtime = StorageRuntimeV48()
    await runtime.warm(db)
    active = runtime.active_users()
    assert len(active) == 1
    assert active[0]["user_id"] == 7
    assert active[0]["location"]["latitude"] == 41.0
    states = runtime.approach_states(7, {"4ba123"})
    assert states["4ba123"]["active"] is True
    await db.close()


@pytest.mark.asyncio
async def test_sqlite_consistent_backup_can_be_reopened(tmp_path: Path):
    source = tmp_path / "source.db"
    target = tmp_path / "backup.db"
    db = SQLiteDatabase(source)
    await db["profiles"].insert_one({"user_id": 1, "profile_id": "home", "name": "Home"})
    await db.backup_to(target)
    assert target.exists()
    await db.close()

    restored = SQLiteDatabase(target)
    row = await restored["profiles"].find_one({"user_id": 1, "profile_id": "home"})
    assert row is not None
    assert row["name"] == "Home"
    ok, _ = await restored.integrity_check()
    assert ok is True
    await restored.close()
