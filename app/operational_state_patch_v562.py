"""Runtime wiring for the v5.6.2 operational-volume boundary.

Installed after the established v4.8 storage guard. It changes persistence
backends only; prediction, qualification, cancellation and alert timing are not
modified.
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app import database
from app.operational_state_v562 import (
    count_notification_history,
    get_notification_history,
    get_photo_snapshot,
    get_system_status,
    load_restart_states,
    persist_approach_state,
    persist_notification_event,
    persist_optional_document,
    persist_photo_snapshot,
    persist_system_status,
    recent_notifications,
)

_INSTALLED = False


async def _durable_mongo_indexes_only(db: Any) -> None:
    """Create indexes only for durable Mongo state; operational collections are retired."""
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
    await db["provider_learning"].create_index([("user_id", 1), ("geohash", 1)], unique=True)
    await db["provider_learning"].create_index("user_id")
    await db["ai_usage"].create_index([("model_name", 1), ("day", 1)], unique=True)
    await db["feedback"].create_index([("user_id", 1), ("notification_id", 1)])
    await db["feedback"].create_index("user_id")
    await db["camera_profiles"].create_index("user_id", unique=True)
    await db["admin_audit"].create_index("created_at")
    await db["admin_audit"].create_index("target_user_id")


async def _load_restart_states_volume(self: Any, db: Any) -> list[dict[str, Any]]:
    del self
    return await load_restart_states(db)


async def _persist_state_volume(
    self: Any,
    db: Any,
    key: tuple[int, str],
    version: int,
    document: dict[str, Any],
) -> bool:
    del db
    clean = dict(document)
    clean.pop("_id", None)
    persisted = await persist_approach_state(clean)
    persisted_updated = persisted.get("updated_at")
    local_updated = clean.get("updated_at")
    if persisted_updated is not None and local_updated is not None:
        try:
            if persisted_updated > local_updated and self._approach_versions.get(key) == version:
                self._cache_state(persisted, dirty=False)
        except TypeError:
            pass
    if self._approach_versions.get(key) == version:
        self._dirty.pop(key, None)
    return True


async def _persist_status_volume(
    self: Any,
    db: Any,
    key: str,
    version: int,
    values: dict[str, Any],
) -> None:
    del db
    await persist_system_status(key, values)
    current = self._status_pending.get(key)
    if current is not None and current[0] == version:
        self._status_pending.pop(key, None)


async def _persist_optional_volume(
    self: Any,
    db: Any,
    key: tuple[str, str],
    queued_mono: float,
    document: dict[str, Any],
) -> None:
    del db
    payload = dict(document)
    delay = max(0.0, time.monotonic() - queued_mono)
    payload["storage_persistence_state"] = "delayed" if delay >= 1.0 else "direct"
    payload["storage_deferred_seconds"] = round(delay, 3)
    await persist_optional_document(key[0], key[1], payload)
    current = self._optional_pending.get(key)
    if current is not None and current[1].get("_id") == document.get("_id"):
        self._optional_pending.pop(key, None)


async def _record_photo_snapshot_volume(
    user_id: int,
    aircraft: Any,
    distance_km: float,
    notification_id: str,
    eta_seconds: float | None,
) -> None:
    if not notification_id:
        return
    notifications = sys.modules.get("app.worker.notifications")
    observer = None
    if notifications is not None:
        observer = await notifications._observer_coordinates(user_id)
    now = datetime.now(timezone.utc)
    values = {
        "user_id": int(user_id),
        "aircraft_icao24": getattr(aircraft, "icao24", "") or "",
        "aircraft_type": getattr(aircraft, "aircraft_type", "") or getattr(aircraft, "display_type", "") or "",
        "callsign": getattr(aircraft, "callsign", "") or "",
        "distance_km": float(distance_km),
        "altitude_m": getattr(aircraft, "altitude", None),
        "speed_ms": getattr(aircraft, "velocity", None),
        "heading_deg": getattr(aircraft, "heading", None),
        "vertical_rate_mps": getattr(aircraft, "vertical_rate_mps", None),
        "position_age_s": getattr(aircraft, "position_age_s", None),
        "latitude": getattr(aircraft, "latitude", None),
        "longitude": getattr(aircraft, "longitude", None),
        "eta_seconds": eta_seconds,
        "captured_at": now,
        "expires_at": now + timedelta(hours=6),
    }
    if observer:
        values["observer_latitude"] = observer[0]
        values["observer_longitude"] = observer[1]
    await persist_photo_snapshot(notification_id, int(user_id), values)


async def _notification_fallback_volume(user_id: int, notification_id: str) -> Any | None:
    if not notification_id:
        return None
    service = sys.modules.get("app.photography.service")
    snapshot = await get_photo_snapshot(notification_id, int(user_id))
    if snapshot is None:
        snapshot = await get_notification_history(notification_id, int(user_id))
    if snapshot is None or service is None:
        return None
    return service._aircraft_from_doc(snapshot)


class _NotificationCursor:
    def __init__(self) -> None:
        self._limit = 50
        self._rows: list[dict[str, Any]] | None = None
        self._index = 0

    def sort(self, *_args: Any, **_kwargs: Any) -> "_NotificationCursor":
        return self

    def limit(self, value: int) -> "_NotificationCursor":
        self._limit = max(1, min(500, int(value)))
        return self

    def __aiter__(self) -> "_NotificationCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._rows is None:
            self._rows = await recent_notifications(self._limit)
        if self._index >= len(self._rows):
            raise StopAsyncIteration
        row = self._rows[self._index]
        self._index += 1
        return row


class _NotificationCollectionProxy:
    async def count_documents(self, _query: dict[str, Any]) -> int:
        return await count_notification_history()

    async def find_one(self, query: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        notification_id = str(query.get("_id") or "")
        user_id = query.get("user_id")
        return await get_notification_history(notification_id, int(user_id) if user_id is not None else None)

    def find(self, _query: dict[str, Any], *_args: Any, **_kwargs: Any) -> _NotificationCursor:
        return _NotificationCursor()

    async def update_one(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("notification_history is volume-backed in Plane Alerts v5.6.2")


class _SystemStatusCollectionProxy:
    async def find_one(self, query: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any] | None:
        return await get_system_status(str(query.get("_id") or ""))

    async def update_one(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("system_status is volume-backed in Plane Alerts v5.6.2")


_NOTIFICATION_PROXY = _NotificationCollectionProxy()
_STATUS_PROXY = _SystemStatusCollectionProxy()


def install_operational_state_v562() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # Prevent index maintenance from recreating retired operational Mongo
    # collections after migration drops them.
    database._ensure_indexes = _durable_mongo_indexes_only
    database.notification_history_col = lambda: _NOTIFICATION_PROXY
    database.system_status_col = lambda: _STATUS_PROXY

    from app import storage_runtime_v48

    storage_runtime_v48.StorageRuntimeV48._load_restart_states = _load_restart_states_volume
    storage_runtime_v48.StorageRuntimeV48._persist_state = _persist_state_volume
    storage_runtime_v48.StorageRuntimeV48._persist_status = _persist_status_volume
    storage_runtime_v48.StorageRuntimeV48._persist_optional = _persist_optional_volume

    # These modules are normally imported before the worker runtime. Patch the
    # live module objects so no compatibility path can write operational data to
    # MongoDB after this installer runs.
    telemetry = sys.modules.get("app.worker.notification_telemetry")
    if telemetry is not None:
        telemetry._persist_event = persist_notification_event

    notifications = sys.modules.get("app.worker.notifications")
    if notifications is not None:
        notifications._record_photo_snapshot = _record_photo_snapshot_volume

    photography = sys.modules.get("app.photography.service")
    if photography is not None:
        photography._notification_fallback = _notification_fallback_volume
        photography.notification_history_col = lambda: _NOTIFICATION_PROXY

    admin_routes = sys.modules.get("app.admin.routes")
    if admin_routes is not None:
        admin_routes.notification_history_col = lambda: _NOTIFICATION_PROXY
        admin_routes.system_status_col = lambda: _STATUS_PROXY

    _INSTALLED = True
