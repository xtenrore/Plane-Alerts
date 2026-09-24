"""Async storage connection, v5.5 migration, schema and bounded diagnostics helpers."""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import monitoring
from pymongo.errors import NetworkTimeout, ServerSelectionTimeoutError, ExecutionTimeout, OperationFailure

from app.config import settings
from app.storage_metrics_v48 import storage_metrics

if TYPE_CHECKING:
    from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)
_client: AsyncIOMotorClient | None = None
_db: Any | None = None
_indexes_ready_for: tuple[str, str] | None = None
_schema_ready_for: tuple[str, str] | None = None
_prediction_lab_migration_ready_for: tuple[str, str] | None = None
_maintenance_task: asyncio.Task[None] | None = None
_maintenance_state: dict[str, Any] = {
    "state": "idle",
    "attempt": 0,
    "last_error": None,
    "completed_at": None,
}


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


def _storage_backend() -> str:
    raw = os.getenv("DATABASE_BACKEND", "").strip().lower()
    if raw in {"local", "sqlite"}:
        return "sqlite"
    if raw in {"mongo", "mongodb", ""}:
        return "mongodb"
    raise RuntimeError("DATABASE_BACKEND must be mongodb or sqlite")


def _mongo_target_label(uri: str) -> str:
    try:
        parsed = urlsplit(uri)
        return f"{parsed.scheme or 'mongodb'}://{parsed.hostname or 'unknown-host'}"
    except Exception:
        return "mongodb://configured-host"


def _sqlite_path() -> str:
    raw = os.getenv("SQLITE_PATH", "").strip()
    if raw:
        return raw
    from app.local_database_v55 import default_sqlite_path

    return str(default_sqlite_path())


def database_backend_name() -> str:
    return _storage_backend()


def database_target_label() -> str:
    if _storage_backend() == "sqlite":
        return "sqlite://local"
    return _mongo_target_label(settings.mongo_uri)


def _index_cache_key() -> tuple[str, str]:
    if _storage_backend() == "sqlite":
        return ("sqlite", _sqlite_path())
    return (_mongo_target_label(settings.mongo_uri), settings.database_name)


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, NetworkTimeout, ServerSelectionTimeoutError, ExecutionTimeout))


def _is_atlas_space_quota(exc: BaseException) -> bool:
    if not isinstance(exc, OperationFailure):
        return False
    if getattr(exc, "code", None) == 8000 and "space quota" in str(exc).lower():
        return True
    return "over your space quota" in str(exc).lower()


def _truthy_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _background_maintenance_enabled() -> bool:
    """Defer expensive Mongo maintenance on Railway unless explicitly overridden.

    v5.5 can need to export tens of thousands of historical Prediction Lab rows.
    That migration is required, but it is low-priority storage work and must not
    hold FastAPI lifespan/readiness or the five-second aircraft monitor hostage.
    """
    explicit = _truthy_env("PLANE_STORAGE_MAINTENANCE_BACKGROUND")
    if explicit is not None:
        return explicit
    return bool(os.getenv("RAILWAY_ENVIRONMENT", "").strip())


def storage_maintenance_snapshot() -> dict[str, Any]:
    snapshot = dict(_maintenance_state)
    snapshot["background_enabled"] = _background_maintenance_enabled()
    snapshot["task_active"] = bool(_maintenance_task is not None and not _maintenance_task.done())
    return snapshot


async def _ensure_indexes_with_quota_bridge(db: Any, key: tuple[str, str]) -> bool:
    """Keep a readable quota-full Atlas DB alive only long enough for the v5.5 archive migration."""
    global _indexes_ready_for
    if _indexes_ready_for == key:
        return True
    try:
        await _ensure_indexes(db)
    except OperationFailure as exc:
        if not _is_atlas_space_quota(exc):
            raise
        logger.warning(
            "MongoDB is at storage quota; deferring normal index maintenance until Prediction Lab migration frees space."
        )
        return False
    _indexes_ready_for = key
    return True


async def _ensure_schema(db: Any, key: tuple[str, str]) -> None:
    global _schema_ready_for
    if _schema_ready_for == key:
        return
    if not callable(getattr(db, "__getitem__", None)):
        logger.debug("Skipping schema migration for non-database test double")
        return
    from app.storage_migrations_v48 import run_migrations

    await run_migrations(db)
    _schema_ready_for = key


async def _ensure_prediction_lab_migration(db: Any, key: tuple[str, str]) -> None:
    """Export and retire only legacy Prediction Lab Mongo collections before normal Mongo maintenance."""
    global _prediction_lab_migration_ready_for
    if _prediction_lab_migration_ready_for == key:
        return
    if key[0] == "sqlite":
        # Fresh/self-host SQLite never contained the legacy high-volume Mongo
        # collections. File-backed Prediction Lab evidence is already native.
        _prediction_lab_migration_ready_for = key
        return
    if not callable(getattr(db, "__getitem__", None)):
        logger.debug("Skipping Prediction Lab migration for non-database test double")
        return

    from app.prediction_lab_files_v55 import migrate_prediction_lab_mongo

    manifest = await migrate_prediction_lab_mongo(db)
    if not bool(manifest.get("verified")) or not bool(manifest.get("legacy_collections_dropped")):
        raise RuntimeError("Prediction Lab Mongo migration did not verify and retire all legacy Lab collections")

    _prediction_lab_migration_ready_for = key
    counts = {
        name: int((details or {}).get("exported_count", 0) or 0)
        for name, details in (manifest.get("collections") or {}).items()
    }
    logger.info("Prediction Lab Mongo migration ready: counts=%s", counts)


async def _prepare_connected_database(db: Any, key: tuple[str, str]) -> None:
    """Complete the v5.5 archive migration, then require normal indexes/schema."""
    await _ensure_prediction_lab_migration(db, key)
    indexes_ready = await _ensure_indexes_with_quota_bridge(db, key)
    if not indexes_ready:
        # The quota bridge exists only to permit the Prediction Lab export/drop.
        # At this point migration already succeeded, so silently starting without
        # normal application indexes would hide a real storage release-gate failure.
        raise RuntimeError("MongoDB remains over storage quota after verified Prediction Lab migration")
    await _ensure_schema(db, key)


async def _background_storage_maintenance(db: Any, key: tuple[str, str]) -> None:
    """Retry migration/index/schema work without blocking live application startup."""
    initial_delay = max(0.0, float(os.getenv("PLANE_STORAGE_MAINTENANCE_INITIAL_DELAY_S", "2") or "2"))
    retry_delay = max(0.1, float(os.getenv("PLANE_STORAGE_MAINTENANCE_RETRY_S", "5") or "5"))
    max_retry_delay = max(retry_delay, float(os.getenv("PLANE_STORAGE_MAINTENANCE_MAX_RETRY_S", "120") or "120"))
    if initial_delay:
        await asyncio.sleep(initial_delay)

    attempt = 0
    delay = retry_delay
    while True:
        attempt += 1
        _maintenance_state.update({"state": "running", "attempt": attempt, "last_error": None})
        try:
            await _prepare_connected_database(db, key)
            _maintenance_state.update(
                {
                    "state": "ready",
                    "last_error": None,
                    "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            )
            logger.info("Background storage maintenance ready after %d attempt(s)", attempt)
            return
        except asyncio.CancelledError:
            _maintenance_state["state"] = "cancelled"
            raise
        except Exception as exc:
            _maintenance_state.update({"state": "retrying", "last_error": type(exc).__name__})
            logger.warning(
                "Background storage maintenance attempt %d failed (%s); retrying in %.1fs",
                attempt,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(max_retry_delay, delay * 2.0)


def _schedule_connected_database_maintenance(db: Any, key: tuple[str, str]) -> None:
    global _maintenance_task
    if _maintenance_task is not None and not _maintenance_task.done():
        return
    _maintenance_state.update({"state": "scheduled", "attempt": 0, "last_error": None, "completed_at": None})
    _maintenance_task = asyncio.create_task(
        _background_storage_maintenance(db, key),
        name="plane-alerts-storage-maintenance-v553",
    )
    logger.info("Scheduled low-priority storage migration/index/schema maintenance in background")


async def _prepare_or_schedule_connected_database(db: Any, key: tuple[str, str]) -> None:
    if key[0] != "sqlite" and _background_maintenance_enabled():
        _schedule_connected_database_maintenance(db, key)
        return
    await _prepare_connected_database(db, key)


async def _connect_sqlite(*, ensure_indexes: bool) -> Any:
    global _db, _indexes_ready_for
    from app.local_database_v55 import SQLiteDatabase

    path = _sqlite_path()
    key = ("sqlite", path)
    storage_metrics.set_state("connecting")
    if _db is None or not bool(getattr(_db, "is_plane_alerts_sqlite", False)):
        _db = SQLiteDatabase(path)
    await _db.command("ping")
    storage_metrics.set_state("healthy")
    if ensure_indexes and _indexes_ready_for != key:
        await _ensure_indexes(_db)
        _indexes_ready_for = key
    if ensure_indexes:
        await _ensure_schema(_db, key)
    logger.info("SQLite storage ready")
    return _db


async def connect_db(
    max_retries: int = 5,
    retry_delay: float = 2.0,
    timeout_ms: int = 10000,
    *,
    ensure_indexes: bool = True,
) -> Any:
    global _client, _db
    if _storage_backend() == "sqlite":
        return await _connect_sqlite(ensure_indexes=ensure_indexes)

    key = _index_cache_key()
    if _client is not None and _db is not None:
        if ensure_indexes:
            await _prepare_or_schedule_connected_database(_db, key)
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
                appname="plane-alerts-v5.5",
                event_listeners=[_COMMAND_LISTENER],
            )
            _db = _client[settings.database_name]
            await _client.admin.command("ping")
            if had_failure:
                storage_metrics.record_reconnect()
            storage_metrics.set_state("healthy")
            logger.info("MongoDB connection established – database: %s", settings.database_name)
            if ensure_indexes:
                await _prepare_or_schedule_connected_database(_db, key)
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
                    type(exc).__name__, retry_delay,
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
    global _client, _db, _indexes_ready_for, _schema_ready_for, _prediction_lab_migration_ready_for, _maintenance_task
    if _maintenance_task is not None and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except asyncio.CancelledError:
            pass
    _maintenance_task = None
    _maintenance_state.update({"state": "idle", "attempt": 0, "last_error": None, "completed_at": None})
    if _db is not None and bool(getattr(_db, "is_plane_alerts_sqlite", False)):
        await _db.close()
        _db = None
    if _client is not None:
        _client.close()
        _client = None
        _db = None
    _indexes_ready_for = None
    _schema_ready_for = None
    _prediction_lab_migration_ready_for = None
    storage_metrics.set_state("closed")
    logger.info("Database connection closed.")


def get_db() -> Any:
    if _db is None:
        raise RuntimeError("Database not initialised – call connect_db() first.")
    return _db


def users_col() -> Any: return get_db()["users"]
def locations_col() -> Any: return get_db()["locations"]
def preferences_col() -> Any: return get_db()["preferences"]
def profiles_col() -> Any: return get_db()["profiles"]
def notification_history_col() -> Any: return get_db()["notification_history"]
def user_state_col() -> Any: return get_db()["user_state"]
def provider_learning_col() -> Any: return get_db()["provider_learning"]
def ai_usage_col() -> Any: return get_db()["ai_usage"]
def feedback_col() -> Any: return get_db()["feedback"]
def camera_profiles_col() -> Any: return get_db()["camera_profiles"]
def system_status_col() -> Any: return get_db()["system_status"]
def admin_audit_col() -> Any: return get_db()["admin_audit"]


async def _ensure_indexes(db: Any) -> None:
    """Ensure indexes for normal application state only; Prediction Lab Mongo is retired by v5.5."""
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
    await db["system_status"].create_index("updated_at")
    await db["admin_audit"].create_index("created_at")
    await db["admin_audit"].create_index("target_user_id")
    logger.info("Database indexes ready.")