from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from motor.motor_asyncio import AsyncIOMotorClient


@pytest.mark.asyncio
async def test_mongo_upgrade_exports_legacy_prediction_lab_and_preserves_user_data(tmp_path: Path) -> None:
    # This is a real MongoDB service-container test, not a Motor mock. Environment
    # is set before importing app.database so Settings and backend selection use
    # the isolated community CI database.
    uri = os.environ["MONGO_URI"]
    database_name = os.environ["DATABASE_NAME"]
    evidence_root = tmp_path / "prediction_lab"
    os.environ["DATABASE_BACKEND"] = "mongodb"
    os.environ["PLANE_STORAGE_MAINTENANCE_BACKGROUND"] = "false"
    os.environ["PREDICTION_LAB_ROOT"] = str(evidence_root)

    seed_client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
    seed_db = seed_client[database_name]
    await seed_db.drop_collection("users")
    await seed_db["users"].insert_one({"user_id": 778899, "is_admin": False, "setup_complete": True})
    from app.prediction_lab_files_v55 import LAB_COLLECTIONS, migration_state

    for index, collection_name in enumerate(LAB_COLLECTIONS):
        await seed_db[collection_name].insert_one(
            {
                "event_id": f"community-upgrade-{index}",
                "captured_at": "2026-09-24T12:00:00Z",
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
    from app.storage_migrations_v48 import EXPECTED_SCHEMA_VERSION

    connected = await database.connect_db(max_retries=2, retry_delay=0.2, timeout_ms=5000)
    try:
        assert database.database_backend_name() == "mongodb"
        assert await database.ping_db(timeout_s=2.0)

        user = await connected["users"].find_one({"user_id": 778899})
        assert user is not None and user["setup_complete"] is True

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
        await database.close_db()
        cleanup = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
        await cleanup.drop_database(database_name)
        cleanup.close()
