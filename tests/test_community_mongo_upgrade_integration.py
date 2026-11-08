from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient


@pytest.mark.asyncio
async def test_v553_to_v588_mongo_upgrade_migrates_operational_data_and_preserves_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is a real MongoDB service-container upgrade test, not a Motor mock.
    uri = os.getenv("MONGO_URI", "").strip()
    database_name = os.getenv("DATABASE_NAME", "").strip()
    if not uri or not database_name:
        pytest.skip("MongoDB integration service is not configured for this test run")

    evidence_root = tmp_path / "prediction_lab"
    monkeypatch.setenv("DATABASE_BACKEND", "mongodb")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_BACKGROUND", "false")
    monkeypatch.setenv("PREDICTION_LAB_ROOT", str(evidence_root))

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    seed_client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    seed_db = seed_client[database_name]
    await seed_db.drop_database(database_name)
    await seed_db["users"].insert_one({"user_id": 778899, "is_admin": False, "setup_complete": True})
    await seed_db["flight_route_samples"].insert_one(
        {
            "callsign": "THY123",
            "utc_date": today,
            "updated_at": now,
            "expires_at": now + timedelta(days=20),
            "aircraft_type": "A320",
            "points": [
                {
                    "captured_at": now.timestamp(),
                    "latitude": 41.1,
                    "longitude": 29.0,
                    "altitude_ft": 10000,
                }
            ],
        }
    )
    await seed_db["notification_history"].insert_one(
        {
            "_id": "legacy-notification-1",
            "user_id": 778899,
            "aircraft_icao24": "abc123",
            "last_event_at": now,
            "events": [{"stage": "sent", "delivered": True}],
        }
    )

    from app.prediction_lab_files_v55 import LAB_COLLECTIONS, migration_state

    for index, collection_name in enumerate(LAB_COLLECTIONS):
        await seed_db[collection_name].insert_one(
            {
                "event_id": f"community-upgrade-{index}",
                "captured_at": now.isoformat(),
                "kind": "community-upgrade-fixture",
                "aircraft_icao24": "abc123",
                "user_id": 778899,
                "telegram_bot_token": "fixture-secret-must-not-export",
                "api_key": "fixture-api-key-must-not-export",
                "sequence": index,
            }
        )
    seed_client.close()

    from app import database
    from app import notification_volume_v563 as notification_volume
    from app import operational_volume_v561 as route_volume
    from app.storage_migrations_v48 import EXPECTED_SCHEMA_VERSION

    connected = await database.connect_db(max_retries=2, retry_delay=0.2, timeout_ms=5000)
    try:
        assert database.database_backend_name() == "mongodb"
        assert await database.ping_db(timeout_s=2.0)

        # Exercise the actual v5.6.1 route-history and v5.6.3 notification-volume
        # migration functions against the service-container MongoDB.
        route_state = await route_volume.migrate_route_history_mongo(connected)
        notification_state = await notification_volume.retire_legacy_mongo()

        user = await connected["users"].find_one({"user_id": 778899})
        assert user is not None and user["setup_complete"] is True

        route_doc = await route_volume.load_route_doc("THY123", today)
        assert route_state["import_complete"] is True
        assert route_state["imported_docs"] == 1
        assert route_state["legacy_collection_dropped"] is True
        assert route_doc is not None
        assert route_doc["aircraft_type"] == "A320"
        assert len(route_doc["points"]) == 1
        assert await connected["flight_route_samples"].count_documents({}) == 0

        notification_doc = await notification_volume.notification_history_collection().find_one(
            {"_id": "legacy-notification-1"}
        )
        assert notification_state["migration_verified"] is True
        assert notification_state["source_count"] == 1
        assert notification_state["imported"] == 1
        assert notification_state["legacy_notification_collection_dropped"] is True
        assert notification_state["legacy_route_collection_dropped"] is True
        assert notification_state["normal_application_data_untouched"] is True
        assert notification_doc is not None
        assert notification_doc["user_id"] == 778899
        assert await connected["notification_history"].count_documents({}) == 0

        state = migration_state(evidence_root)
        assert state["verified"] is True
        assert state["legacy_collections_dropped"] is True
        for collection_name in LAB_COLLECTIONS:
            assert state["collections"][collection_name]["source_count"] == 1
            assert state["collections"][collection_name]["exported_count"] == 1
            assert await connected[collection_name].count_documents({}) == 0

        migration = await connected["schema_migrations"].find_one({"version": EXPECTED_SCHEMA_VERSION})
        assert migration is not None

        archive_files = sorted((evidence_root / "archive" / "mongo-import").rglob("*.ndjson"))
        assert len(archive_files) == len(LAB_COLLECTIONS)
        archive_text = "\n".join(path.read_text(encoding="utf-8") for path in archive_files)
        assert "fixture-secret-must-not-export" not in archive_text
        assert "fixture-api-key-must-not-export" not in archive_text
        assert "telegram_bot_token" not in archive_text.lower()
        assert "api_key" not in archive_text.lower()
        assert "observer_ref" in archive_text
        manifest = json.loads((evidence_root / "archive" / "mongo-import" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["verified"] is True
        assert manifest["normal_application_data_untouched"] is True
    finally:
        await notification_volume.close()
        await database.close_db()
        cleanup = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        await cleanup.drop_database(database_name)
        cleanup.close()
