"""Plane Alerts v5.6.3 persistent-volume notification telemetry.

Notification lifecycle telemetry is operational evidence, not user configuration.
It therefore lives beside route history and Prediction Lab evidence on the Plane
Alerts persistent volume and must never use MongoDB for normal runtime writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.local_database_v55 import SQLiteDatabase
from app.prediction_lab_files_v55 import _atomic_write, ensure_layout, root_path

logger = logging.getLogger(__name__)

RETENTION_DAYS = 35
MIGRATION_STATE = "notification_volume_migration_v563.json"
LEGACY_COLLECTION = "notification_history"
LEGACY_ROUTE_COLLECTION = "flight_route_samples"

_db: SQLiteDatabase | None = None
_ready = False
_ready_lock: asyncio.Lock | None = None
_retirement_task: asyncio.Task | None = None


def database_path() -> Path:
    override = os.getenv("NOTIFICATION_TELEMETRY_DB_PATH", "").strip()
    if override:
        return Path(override).expanduser()
    return root_path() / "runtime" / "notification_history.sqlite3"


def notification_history_collection():
    global _db
    if _db is None:
        _db = SQLiteDatabase(database_path())
    return _db["notification_history"]


async def ensure_ready() -> None:
    global _ready, _ready_lock
    if _ready:
        return
    if _ready_lock is None:
        _ready_lock = asyncio.Lock()
    async with _ready_lock:
        if _ready:
            return
        collection = notification_history_collection()
        await collection.create_index("_id", unique=True, name="notification_id_unique")
        await collection.create_index([("user_id", 1), ("last_event_at", -1)], name="user_last_event")
        await collection.create_index("expires_at", expireAfterSeconds=0, name="expires_at_ttl")
        _ready = True
        logger.info(
            "Notification telemetry volume ready path=%s retention_days=%d mongo_runtime_writes=disabled",
            database_path(),
            RETENTION_DAYS,
        )


def retention_deadline(occurred_at: Any) -> datetime:
    if not isinstance(occurred_at, datetime):
        occurred_at = datetime.now(timezone.utc)
    elif occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    else:
        occurred_at = occurred_at.astimezone(timezone.utc)
    return occurred_at + timedelta(days=RETENTION_DAYS)


def _state_path() -> Path:
    return ensure_layout() / "state" / MIGRATION_STATE


def _read_state() -> dict[str, Any]:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}


def _write_state(state: dict[str, Any]) -> None:
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write(_state_path(), payload)


async def _import_legacy_notifications(mongo_db: Any) -> tuple[int, int]:
    source = mongo_db[LEGACY_COLLECTION]
    source_count = int(await source.count_documents({}))

    imported = 0
    cursor = source.find({})
    if hasattr(cursor, "batch_size"):
        cursor = cursor.batch_size(100)
    target = notification_history_collection()
    async for raw in cursor:
        doc = dict(raw)
        notification_id = str(doc.get("_id") or doc.get("notification_id") or "").strip()
        if not notification_id:
            continue
        doc["_id"] = notification_id
        last_event = doc.get("last_event_at") or doc.get("first_notified_at") or datetime.now(timezone.utc)
        doc["expires_at"] = retention_deadline(last_event)
        await target.replace_one({"_id": notification_id}, doc, upsert=True)
        imported += 1
    return source_count, imported


async def retire_legacy_mongo() -> dict[str, Any]:
    """Migrate notification telemetry once, then retire only operational collections."""
    await ensure_ready()
    from app.database import get_db

    mongo_db = get_db()
    previous = await asyncio.to_thread(_read_state)
    source_count = int(previous.get("source_count", -1) or -1)
    imported = int(previous.get("imported", 0) or 0)
    migration_verified = bool(previous.get("migration_verified"))

    if not migration_verified:
        source_count, imported = await _import_legacy_notifications(mongo_db)
        migration_verified = source_count >= 0 and imported == source_count
        if not migration_verified:
            raise RuntimeError(
                f"notification telemetry migration count mismatch source={source_count} imported={imported}"
            )

    # These two names are explicitly operational and reconstructable. No user,
    # location, profile, preference, alert-state or configuration collection is
    # enumerated here.
    await mongo_db[LEGACY_COLLECTION].drop()
    await mongo_db[LEGACY_ROUTE_COLLECTION].drop()

    state = {
        "schema": "plane-alerts-notification-volume-migration-v563",
        "source_count": source_count,
        "imported": imported,
        "migration_verified": True,
        "legacy_notification_collection_dropped": True,
        "legacy_route_collection_dropped": True,
        "mongo_runtime_writes_disabled": True,
        "normal_application_data_untouched": True,
        "volume_path": str(database_path()),
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    await asyncio.to_thread(_write_state, state)
    logger.info(
        "Operational Mongo telemetry retired collections=%s,%s imported_notifications=%d user_data_untouched=true",
        LEGACY_COLLECTION,
        LEGACY_ROUTE_COLLECTION,
        imported,
    )
    return state


async def _retirement_runner() -> None:
    delay = 5.0
    while True:
        try:
            await retire_legacy_mongo()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "notification_volume_mongo_retirement_failed error=%s retry_in=%.0fs",
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(120.0, delay * 2.0)


def schedule_legacy_retirement() -> None:
    global _retirement_task
    if _retirement_task is not None and not _retirement_task.done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _retirement_task = loop.create_task(_retirement_runner(), name="notification-volume-mongo-retirement")


async def initialize() -> None:
    await ensure_ready()
    schedule_legacy_retirement()


async def delivery_counts_since(cutoff: datetime) -> dict[str, int]:
    await ensure_ready()
    collection = notification_history_collection()
    rows = [doc async for doc in collection.find({"last_event_at": {"$gte": cutoff}})]
    first = 0
    cancelled = 0
    failed = 0
    for doc in rows:
        first_at = doc.get("first_notified_at")
        if isinstance(first_at, datetime) and first_at >= cutoff:
            first += 1
        events = doc.get("events") if isinstance(doc.get("events"), list) else []
        if any(
            isinstance(event, dict)
            and event.get("event_type") == "cancellation_update"
            and isinstance(event.get("occurred_at"), datetime)
            and event["occurred_at"] >= cutoff
            for event in events
        ):
            cancelled += 1
        if any(
            isinstance(event, dict)
            and event.get("event_type") == "failed_delivery"
            and isinstance(event.get("occurred_at"), datetime)
            and event["occurred_at"] >= cutoff
            for event in events
        ):
            failed += 1
    return {
        "notification_records_first_delivered": first,
        "notification_records_with_cancellation_update": cancelled,
        "notification_records_with_failed_delivery": failed,
    }


async def close() -> None:
    global _db, _ready, _retirement_task, _ready_lock
    if _retirement_task is not None and not _retirement_task.done():
        _retirement_task.cancel()
        await asyncio.gather(_retirement_task, return_exceptions=True)
    _retirement_task = None
    if _db is not None:
        await _db.close()
    _db = None
    _ready = False
    _ready_lock = None
