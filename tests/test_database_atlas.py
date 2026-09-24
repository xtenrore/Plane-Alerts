import pytest
from pymongo.errors import OperationFailure

import app.database as database
from app.database import _mongo_target_label


def test_atlas_log_label_hides_credentials():
    uri = "mongodb+srv://plane_user:super-secret@cluster0.example.mongodb.net/?retryWrites=true&w=majority"
    label = _mongo_target_label(uri)
    assert label == "mongodb+srv://cluster0.example.mongodb.net"
    assert "plane_user" not in label
    assert "super-secret" not in label


def test_local_mongo_log_label_is_safe():
    assert _mongo_target_label("mongodb://localhost:27017") == "mongodb://localhost"


@pytest.mark.asyncio
async def test_hot_connect_reuses_existing_db_without_index_scan(monkeypatch):
    existing_db = object()
    monkeypatch.setattr(database, "_client", object())
    monkeypatch.setattr(database, "_db", existing_db)

    calls = 0

    async def fake_indexes(db):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(database, "_ensure_indexes", fake_indexes)

    result = await database.connect_db(ensure_indexes=False)
    assert result is existing_db
    assert calls == 0


@pytest.mark.asyncio
async def test_index_scan_is_cached_for_warm_connection(monkeypatch):
    existing_db = object()
    monkeypatch.setattr(database, "_client", object())
    monkeypatch.setattr(database, "_db", existing_db)
    monkeypatch.setattr(database, "_indexes_ready_for", None)

    calls = 0

    async def fake_indexes(db):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(database, "_ensure_indexes", fake_indexes)

    first = await database.connect_db(ensure_indexes=True)
    second = await database.connect_db(ensure_indexes=True)
    assert first is existing_db
    assert second is existing_db
    assert calls == 1


@pytest.mark.asyncio
async def test_prediction_lab_migration_precedes_normal_indexes_and_schema(monkeypatch):
    import app.prediction_lab_files_v55 as prediction_lab_files

    class FakeDB:
        def __getitem__(self, name):
            return name

    db = FakeDB()
    key = database._index_cache_key()
    events = []
    monkeypatch.setattr(database, "_client", object())
    monkeypatch.setattr(database, "_db", db)
    monkeypatch.setattr(database, "_prediction_lab_migration_ready_for", None)
    monkeypatch.setattr(database, "_indexes_ready_for", None)
    monkeypatch.setattr(database, "_schema_ready_for", None)

    async def migrate(candidate):
        assert candidate is db
        events.append("migration")
        return {"verified": True, "legacy_collections_dropped": True, "collections": {}}

    async def indexes(candidate):
        assert candidate is db
        events.append("indexes")

    async def schema(candidate, candidate_key):
        assert candidate is db and candidate_key == key
        events.append("schema")

    monkeypatch.setattr(prediction_lab_files, "migrate_prediction_lab_mongo", migrate)
    monkeypatch.setattr(database, "_ensure_indexes", indexes)
    monkeypatch.setattr(database, "_ensure_schema", schema)

    result = await database.connect_db(ensure_indexes=True)
    assert result is db
    assert events == ["migration", "indexes", "schema"]


@pytest.mark.asyncio
async def test_unverified_prediction_lab_migration_blocks_storage_startup(monkeypatch):
    import app.prediction_lab_files_v55 as prediction_lab_files

    class FakeDB:
        def __getitem__(self, name):
            return name

    db = FakeDB()
    key = database._index_cache_key()
    monkeypatch.setattr(database, "_prediction_lab_migration_ready_for", None)

    async def migrate(candidate):
        assert candidate is db
        return {"verified": False, "legacy_collections_dropped": False, "collections": {}}

    monkeypatch.setattr(prediction_lab_files, "migrate_prediction_lab_mongo", migrate)
    with pytest.raises(RuntimeError, match="did not verify"):
        await database._ensure_prediction_lab_migration(db, key)


@pytest.mark.asyncio
async def test_quota_full_index_failure_is_deferred_only_for_prediction_lab_migration(monkeypatch):
    db = object()
    key = ("mongodb+srv://cluster.example.mongodb.net", "plane_alerts")
    monkeypatch.setattr(database, "_indexes_ready_for", None)

    async def quota_failure(candidate):
        assert candidate is db
        raise OperationFailure("you are over your space quota, using 512 MB of 512 MB", code=8000)

    monkeypatch.setattr(database, "_ensure_indexes", quota_failure)
    assert await database._ensure_indexes_with_quota_bridge(db, key) is False
    assert database._indexes_ready_for is None


@pytest.mark.asyncio
async def test_post_migration_quota_failure_is_fatal(monkeypatch):
    db = object()
    key = ("mongodb+srv://cluster.example.mongodb.net", "plane_alerts")

    async def migration(candidate, candidate_key):
        assert candidate is db and candidate_key == key

    async def quota_bridge(candidate, candidate_key):
        assert candidate is db and candidate_key == key
        return False

    monkeypatch.setattr(database, "_ensure_prediction_lab_migration", migration)
    monkeypatch.setattr(database, "_ensure_indexes_with_quota_bridge", quota_bridge)
    with pytest.raises(RuntimeError, match="remains over storage quota"):
        await database._prepare_connected_database(db, key)


@pytest.mark.asyncio
async def test_non_quota_index_failure_remains_fatal(monkeypatch):
    db = object()
    key = ("mongodb+srv://cluster.example.mongodb.net", "plane_alerts")

    async def other_failure(candidate):
        raise OperationFailure("not authorized", code=13)

    monkeypatch.setattr(database, "_ensure_indexes", other_failure)
    with pytest.raises(OperationFailure):
        await database._ensure_indexes_with_quota_bridge(db, key)
