"""Mongo dynamic collection attributes must never be tested for truthiness."""
from __future__ import annotations

import pytest

from app import database, local_database_v55, operational_storage_policy_v563 as policy


class MotorCollectionLike:
    def __bool__(self):
        raise NotImplementedError("Motor collections do not implement truth testing")


class MotorDatabaseLike:
    def __init__(self):
        self.indexed = []

    def __getattr__(self, name):
        if name == "is_plane_alerts_sqlite":
            return MotorCollectionLike()
        raise AttributeError(name)

    def __getitem__(self, name):
        owner = self

        class Collection:
            async def create_index(self, *args, **kwargs):
                owner.indexed.append(name)

        return Collection()


@pytest.mark.asyncio
async def test_background_maintenance_reaches_indexes_and_schema_with_motor_dynamic_attribute(monkeypatch):
    mongo = MotorDatabaseLike()
    key = ("mongodb://configured-host", "aircraft_bot")
    stages = []

    async def original_indexes(db):
        await db["users"].create_index("user_id")
        await db["flight_route_samples"].create_index("utc_date")
        stages.append("indexes")

    async def schema(db, actual_key):
        assert db is mongo and actual_key == key
        stages.append("schema")

    monkeypatch.setattr(policy, "_original_ensure_indexes", original_indexes)
    monkeypatch.setattr(policy, "_volume_authoritative_with_notification_retirement", lambda: True)
    monkeypatch.setattr(database, "_ensure_indexes", policy._ensure_indexes_without_operational_mongo)
    monkeypatch.setattr(database, "_ensure_prediction_lab_migration", policy._ensure_prediction_lab_migration_without_retired_mongo)
    monkeypatch.setattr(database, "_ensure_schema", schema)
    monkeypatch.setattr(database, "_indexes_ready_for", None)
    monkeypatch.setattr(database, "_prediction_lab_migration_ready_for", None)

    await database._prepare_connected_database(mongo, key)

    assert stages == ["indexes", "schema"]
    assert mongo.indexed == ["users"]


@pytest.mark.asyncio
async def test_motor_dynamic_attribute_does_not_break_shutdown_or_backend_label(monkeypatch):
    mongo = MotorDatabaseLike()

    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(database, "_db", mongo)
    monkeypatch.setattr(database, "_client", client)
    monkeypatch.setattr(database, "_maintenance_task", None)

    assert local_database_v55.backend_label(mongo) == "mongodb"
    await database.close_db()
    assert client.closed is True
    assert database._db is None


@pytest.mark.asyncio
async def test_switching_to_local_storage_does_not_truth_test_motor_collection(monkeypatch, tmp_path):
    from app.local_database_v55 import SQLiteDatabase

    monkeypatch.setattr(database, "_db", MotorDatabaseLike())
    monkeypatch.setattr(database, "_client", None)
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "plane.sqlite3"))

    local = await database._connect_sqlite(ensure_indexes=False)
    assert isinstance(local, SQLiteDatabase)
    await database.close_db()


def test_sqlite_marker_remains_supported():
    class LocalDatabase:
        is_plane_alerts_sqlite = True

    assert local_database_v55.backend_label(LocalDatabase()) == "sqlite"
