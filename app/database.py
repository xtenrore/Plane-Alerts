"""Async MongoDB connection, schema and bounded diagnostics helpers."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import monitoring
from pymongo.errors import NetworkTimeout, ServerSelectionTimeoutError, ExecutionTimeout

from app.config import settings
from app.storage_metrics_v48 import storage_metrics

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)
_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None
_indexes_ready_for: tuple[str, str] | None = None
_schema_ready_for: tuple[str, str] | None = None


class _StorageCommandListener(monitoring.CommandListener):
    """Collect latency/failure distributions without retaining commands/documents."""

    def started(self, event: Any) -> None:
        return None

    def succeeded(self, event: Any) -> None:
        storage_metrics.record_command(
            str(getattr(event, "command_name", "") or ""),
            float(getattr(event, "duration_micros", 0.0) or 0.0) / 1000.0,
            True,
        )

    def failed(self, event: Any) -> None:
        failure = getattr(event, "failure", None)
        timed_out = isinstance(
            failure,
            (NetworkTimeout, ServerSelectionTimeoutError, ExecutionTimeout, asyncio.TimeoutError),
        )
        storage_metrics.record_command(
            str(getattr(event, "command_name", "") or ""),
            float(getattr(event, "duration_micros", 0.0) or 0.0) / 1000.0,
            False,
            timeout=timed_out,
        )


_COMMAND_LISTENER = _StorageCommandListener()


def _mongo_target_label(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
        return f"{parsed.scheme or 'mongodb'}://{parsed.hostname or 'unknown-host'}"
    except Exception:
        return "mongodb://configured-host"


def _index_cache_key() -> tuple[str, str]:
    return (_mongo_target_label(settings.mongo_uri), settings.database_name)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, NetworkTimeout, ServerSelectionTimeoutError, ExecutionTimeout))


async def _ensure_schema(db: Any, key: tuple[str, str]) -> None:
    """Run v4.8 migrations once for a real Mongo-style database handle.

    Some legacy unit tests intentionally inject opaque sentinel objects to verify
    connection/index caching. Those objects are not database implementations and
    cannot support schema operations, so they must not be mistaken for Mongo.
    Production Motor database objects always implement collection access.
    """
    global _schema_ready_for
    if _schema_ready_for == key:
        return
    if not callable(getattr(db, "__getitem__", None)):
        logger.debug("Skipping schema migration for non-database test double")
        return
    from app.storage_migrations_v48 import run_migrations

    await run_migrations(db)
    _schema_ready_for = key


async def connect_db(
    max_retries: int = 5,
    retry_delay: float = 2.0,
    timeout_ms: int = 10000,
    *,
    ensure_indexes: bool = True,
) -> AsyncIOMotorDatabase:
    global _client, _db, _indexes_ready_for
    key = _index_cache_key()
    if _client is not None and _db is not None:
        if ensure_indexes and _indexes_ready_for != key:
            await _ensure_indexes(_db)
            _indexes_ready_for = key
        if ensure_indexes:
            await _ensure_schema(_db, key)
        return _db

    target = _mongo_target_label(settings.mongo_uri)
    had_failure = False
    storage_metrics.set_state("connecting")
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            storage_metrics.record_retry()
        try:
            logger.info("Connecting to MongoDB at %s (attempt %d/%d) …", target, attempt, max_retries)
            _client = AsyncIOMotorClient(
                settings.mongo_uri,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
                socketTimeoutMS=max(timeout_ms, 2000),
                maxPoolSize=5,
                minPoolSize=0,
                maxIdleTimeMS=60000,
                appname="plane-alerts-v4.8",
                event_listeners=[_COMMAND_LISTENER],
            )
            _db = _client[settings.database_name]
            await _client.admin.command("ping")
            if had_failure:
                storage_metrics.record_reconnect()
            storage_metrics.set_state("healthy")
            logger.info("MongoDB connection established – database: %s", settings.database_name)
            if ensure_indexes and _indexes_ready_for != key:
                await _ensure_indexes(_db)
                _indexes_ready_for = key
            if ensure_indexes:
                await _ensure_schema(_db, key)
            return _db
        except Exception as exc:
            had_failure = True
            storage_metrics.record_command("connect", 0.0, False, timeout=_is_timeout(exc))
            storage_metrics.set_state("unavailable")
            if _client is not None:
                _client.close()
                _client = None
                _db = None
            if attempt < max_retries:
                logger.warning(
                    "Failed to connect to MongoDB (%s). Retrying in %.1fs...",
                    type(exc).__name__,
                    retry_delay,
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    "Could not connect to MongoDB after %d attempts: %s",
                    max_retries,
                    type(exc).__name__,
                )
                raise


async def ping_db(*, timeout_s: float = 1.0) -> bool:
    """Bounded database liveness probe used for diagnostics, never alert gating."""
    if _db is None:
        storage_metrics.set_state("unavailable")
        return False
    try:
        await asyncio.wait_for(_db.command("ping"), timeout=max(0.05, float(timeout_s)))
        storage_metrics.set_state("healthy")
        return True
    except Exception as exc:
        storage_metrics.record_command("ping", timeout_s * 1000.0, False, timeout=_is_timeout(exc))
        storage_metrics.set_state("degraded")
        return False


async def close_db() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
        storage_metrics.set_state("closed")
        logger.info("MongoDB connection closed.")


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised – call connect_db() first.")
    return _db


def users_col() -> AsyncIOMotorCollection: return get_db()["users"]
def locations_col() -> AsyncIOMotorCollection: return get_db()["locations"]
def preferences_col() -> AsyncIOMotorCollection: return get_db()["preferences"]
def profiles_col() -> AsyncIOMotorCollection: return get_db()["profiles"]
def notification_history_col() -> AsyncIOMotorCollection: return get_db()["notification_history"]
def user_state_col() -> AsyncIOMotorCollection: return get_db()["user_state"]
def provider_learning_col() -> AsyncIOMotorCollection: return get_db()["provider_learning"]
def ai_usage_col() -> AsyncIOMotorCollection: return get_db()["ai_usage"]
def feedback_col() -> AsyncIOMotorCollection: return get_db()["feedback"]
def camera_profiles_col() -> AsyncIOMotorCollection: return get_db()["camera_profiles"]
def system_status_col() -> AsyncIOMotorCollection: return get_db()["system_status"]
def admin_audit_col() -> AsyncIOMotorCollection: return get_db()["admin_audit"]


async def _ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    logger.info("Ensuring database indexes …")
    await db["users"].create_index("user_id", unique=True)
    await db["users"].create_index("username")
    await db["users"].create_index("last_active")
    await db["users"].create_index("is_admin")
    await db["users"].create_index("admin_controls.priority_enabled")
    await db["locations"].create_index("user_id")
    await db["locations"].create_index("geohash")
    await db["preferences"].create_index("user_id", unique=True)
    await db["profiles"].create_index([("user_id", 1), ("profile_id", 1)], unique=True)
    await db["profiles"].create_index([("user_id", 1), ("created_at", 1)])
    await db["user_state"].create_index("user_id", unique=True)
    await db["notification_history"].create_index([("user_id", 1), ("aircraft_icao24", 1), ("cooldown_until", 1)])
    await db["notification_history"].create_index("cooldown_until", expireAfterSeconds=86400)
    await db["provider_learning"].create_index([("user_id", 1), ("geohash", 1)], unique=True)
    await db["provider_learning"].create_index("user_id")
    await db["ai_usage"].create_index([("model_name", 1), ("day", 1)], unique=True)
    await db["feedback"].create_index([("user_id", 1), ("notification_id", 1)])
    await db["feedback"].create_index("user_id")
    await db["camera_profiles"].create_index("user_id", unique=True)
    await db["photo_alert_snapshots"].create_index([("user_id", 1), ("aircraft_icao24", 1)])
    await db["photo_alert_snapshots"].create_index("expires_at", expireAfterSeconds=0)
    await db["approach_states"].create_index([("user_id", 1), ("aircraft_icao24", 1)], unique=True)
    await db["approach_states"].create_index("expires_at", expireAfterSeconds=0)
    await db["flight_route_samples"].create_index([("callsign", 1), ("utc_date", 1)], unique=True)
    await db["flight_route_samples"].create_index("utc_date")
    await db["flight_route_samples"].create_index("expires_at", expireAfterSeconds=0)
    await db["flight_route_samples"].create_index("updated_at")
    await db["prediction_lab_audit"].create_index([("kind", 1), ("status", 1), ("utc_date", 1)])
    await db["prediction_lab_audit"].create_index([("kind", 1), ("status", 1), ("window_end", 1)])
    await db["prediction_lab_audit"].create_index("expires_at", expireAfterSeconds=0)
    await db["prediction_sentinel_routes"].create_index("utc_date")
    await db["system_status"].create_index("updated_at")
    await db["admin_audit"].create_index("created_at")
    await db["admin_audit"].create_index("target_user_id")
    logger.info("Database indexes ready.")
