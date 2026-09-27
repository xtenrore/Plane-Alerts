import pytest

from app import database
from app import operational_storage_policy_v563 as policy


@pytest.mark.asyncio
async def test_verified_retirement_skips_superseded_prediction_lab_mongo_export(monkeypatch):
    key = ("mongodb://atlas.example", "plane_alerts")
    calls = {"legacy": 0, "retirement": 0}

    async def legacy_export(_db, _key):
        calls["legacy"] += 1
        raise NotImplementedError("retired operational Mongo must not be touched")

    def retirement_verified():
        calls["retirement"] += 1
        return True

    monkeypatch.setattr(policy, "_original_ensure_prediction_lab_migration", legacy_export)
    monkeypatch.setattr(policy, "_volume_authoritative_with_notification_retirement", retirement_verified)
    monkeypatch.setattr(database, "_prediction_lab_migration_ready_for", None)

    await policy._ensure_prediction_lab_migration_without_retired_mongo(object(), key)

    assert calls == {"legacy": 0, "retirement": 1}
    assert database._prediction_lab_migration_ready_for == key


@pytest.mark.asyncio
async def test_unverified_retirement_still_runs_safe_legacy_migration(monkeypatch):
    key = ("mongodb://atlas.example", "plane_alerts")
    calls = {"legacy": 0}

    async def legacy_export(db, actual_key):
        assert db is sentinel_db
        assert actual_key == key
        calls["legacy"] += 1

    monkeypatch.setattr(policy, "_original_ensure_prediction_lab_migration", legacy_export)
    monkeypatch.setattr(policy, "_volume_authoritative_with_notification_retirement", lambda: False)
    sentinel_db = object()

    await policy._ensure_prediction_lab_migration_without_retired_mongo(sentinel_db, key)

    assert calls["legacy"] == 1


@pytest.mark.asyncio
async def test_sqlite_never_uses_mongo_retirement_shortcut(monkeypatch):
    key = ("sqlite", "/tmp/plane-alerts.db")
    calls = {"legacy": 0, "retirement": 0}

    async def legacy_export(_db, actual_key):
        assert actual_key == key
        calls["legacy"] += 1

    def retirement_verified():
        calls["retirement"] += 1
        return True

    monkeypatch.setattr(policy, "_original_ensure_prediction_lab_migration", legacy_export)
    monkeypatch.setattr(policy, "_volume_authoritative_with_notification_retirement", retirement_verified)

    await policy._ensure_prediction_lab_migration_without_retired_mongo(object(), key)

    assert calls == {"legacy": 1, "retirement": 0}
