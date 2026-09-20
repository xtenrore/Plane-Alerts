"""Plane Alerts v4.8 in-process storage resilience runtime.

The live aircraft loop consumes verified in-memory configuration and encounter
state. MongoDB refresh and persistence happen on bounded background loops so a
slow analytics/persistence dependency cannot stretch the five-second alert path.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
import logging
import time
from types import SimpleNamespace
from typing import Any

from app.storage_metrics_v48 import storage_metrics

logger = logging.getLogger(__name__)

_CONFIG_REFRESH_S = 5.0
_CONFIG_REFRESH_TIMEOUT_S = 1.5
_FLUSH_IDLE_S = 0.25
_FLUSH_FAILURE_BACKOFF_S = 3.0
_WRITE_TIMEOUT_S = 1.25
_APPROACH_CACHE_MAX = 4096
_APPROACH_DIRTY_MAX = 4096
_STATUS_PENDING_MAX = 32
_OPTIONAL_PENDING_MAX = 512


def _aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _is_newer(left: Any, right: Any) -> bool:
    """Return True when left is a strictly newer datetime than right."""
    ldt = _aware_datetime(left)
    rdt = _aware_datetime(right)
    return bool(ldt is not None and (rdt is None or ldt > rdt))


def _state_key(document: dict[str, Any]) -> tuple[int, str] | None:
    try:
        uid = int(document.get("user_id"))
    except (TypeError, ValueError):
        return None
    icao = str(document.get("aircraft_icao24") or "").strip()
    return (uid, icao) if icao else None


def _apply_mongo_style_update(document: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    out = dict(document)
    for key, value in (update.get("$set") or {}).items():
        out[str(key)] = value
    for key in (update.get("$unset") or {}):
        out.pop(str(key), None)
    for key, value in (update.get("$inc") or {}).items():
        out[str(key)] = (out.get(str(key), 0) or 0) + value
    return out


class StorageRuntimeV48:
    """Bounded last-known-good cache plus coalesced persistence queues."""

    def __init__(self) -> None:
        self._users: dict[int, dict[str, Any]] = {}
        self._config_loaded = False
        self._last_config_refresh_mono = 0.0
        self._config_invalidated = True

        self._approach: OrderedDict[tuple[int, str], dict[str, Any]] = OrderedDict()
        self._approach_versions: dict[tuple[int, str], int] = {}
        self._dirty: OrderedDict[tuple[int, str], int] = OrderedDict()

        self._status_pending: OrderedDict[str, tuple[int, dict[str, Any]]] = OrderedDict()
        self._status_versions: dict[str, int] = {}
        self._optional_pending: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

        self._refresh_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        self._closed = False
        self._flush_failures = 0
        self._next_flush_attempt_mono = 0.0

    @property
    def config_loaded(self) -> bool:
        return self._config_loaded

    @property
    def monitoring_safe(self) -> bool:
        """A cold process is not safe until at least one verified config load succeeds."""
        return self._config_loaded

    @staticmethod
    def _db() -> Any:
        from app.database import get_db

        return get_db()

    async def _load_active_users(self, db: Any) -> dict[int, dict[str, Any]]:
        controls: dict[int, dict[str, Any]] = {}
        ids: list[int] = []
        cursor = db["users"].find(
            {"setup_complete": True},
            {"user_id": 1, "admin_controls": 1},
        )
        async for row in cursor:
            uid = int(row["user_id"])
            ids.append(uid)
            controls[uid] = dict(row.get("admin_controls") or {})
        if not ids:
            return {}

        locations: dict[int, dict[str, Any]] = {}
        preferences: dict[int, dict[str, Any]] = {}
        async for row in db["locations"].find({"user_id": {"$in": ids}}):
            locations[int(row["user_id"])] = row
        async for row in db["preferences"].find({"user_id": {"$in": ids}}):
            preferences[int(row["user_id"])] = row

        out: dict[int, dict[str, Any]] = {}
        for uid in ids:
            loc = locations.get(uid)
            prefs = preferences.get(uid)
            if not loc or not prefs:
                continue
            # Profiles are materialized atomically with one shared revision.
            # Never publish a mixed old/new location-preference pair.
            if loc.get("config_revision") != prefs.get("config_revision"):
                continue
            out[uid] = {
                "user_id": uid,
                "location": loc,
                "preferences": prefs,
                "admin_control": controls.get(uid, {}),
            }
        return out

    async def _load_restart_states(self, db: Any) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        cursor = db["approach_states"].find(
            {"$or": [{"active": True}, {"expires_at": {"$gt": now}}]}
        )
        rows: list[dict[str, Any]] = []
        async for row in cursor:
            rows.append(row)
            if len(rows) >= _APPROACH_CACHE_MAX:
                break
        return rows

    async def warm(self, db: Any | None = None) -> None:
        """Load the minimum state needed for safe live monitoring and restart dedupe."""
        db = self._db() if db is None else db
        users, states = await asyncio.wait_for(
            asyncio.gather(self._load_active_users(db), self._load_restart_states(db)),
            timeout=max(2.5, _CONFIG_REFRESH_TIMEOUT_S * 2.0),
        )
        self._users = users
        self._config_loaded = True
        self._config_invalidated = False
        self._last_config_refresh_mono = time.monotonic()
        for document in states:
            self._cache_state(document, dirty=False)
        storage_metrics.set_state("healthy")
        logger.info(
            "storage_v48_warm users=%d encounter_states=%d",
            len(self._users),
            len(self._approach),
        )

    async def refresh_config_once(self, db: Any | None = None) -> bool:
        """Replace the user snapshot only after a complete successful read."""
        try:
            db = self._db() if db is None else db
            users = await asyncio.wait_for(
                self._load_active_users(db), timeout=_CONFIG_REFRESH_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            storage_metrics.set_state("degraded")
            logger.warning("storage_v48_config_refresh_failed error=%s", type(exc).__name__)
            return False

        self._users = users
        self._config_loaded = True
        self._config_invalidated = False
        self._last_config_refresh_mono = time.monotonic()
        storage_metrics.set_state("healthy")
        return True

    def invalidate_user_config(self, user_id: int | None = None) -> None:
        # We intentionally keep last-known-good data until the refresh succeeds.
        # This flag only accelerates the next DB refresh; it never deletes the
        # configuration that keeps an existing user monitorable during outage.
        self._config_invalidated = True

    def active_users(self) -> list[dict[str, Any]]:
        users: list[dict[str, Any]] = []
        for item in self._users.values():
            users.append(
                {
                    "user_id": int(item["user_id"]),
                    "location": dict(item.get("location") or {}),
                    "preferences": deepcopy(item.get("preferences") or {}),
                    "admin_control": dict(item.get("admin_control") or {}),
                }
            )
        return users

    def approach_states(self, user_id: int, icao24s: list[str] | set[str]) -> dict[str, dict[str, Any]]:
        self._prune_states()
        out: dict[str, dict[str, Any]] = {}
        for raw in icao24s:
            icao = str(raw or "").strip()
            key = (int(user_id), icao)
            row = self._approach.get(key)
            if row is not None:
                self._approach.move_to_end(key)
                out[icao] = dict(row)
        return out

    def _cache_state(self, document: dict[str, Any], *, dirty: bool) -> tuple[int, str] | None:
        key = _state_key(document)
        if key is None:
            return None
        existing = self._approach.get(key)
        # A background refresh/recovery read must never replace newer live state.
        if existing is not None and _is_newer(existing.get("updated_at"), document.get("updated_at")):
            return key
        self._approach[key] = dict(document)
        self._approach.move_to_end(key)
        if dirty:
            version = self._approach_versions.get(key, 0) + 1
            self._approach_versions[key] = version
            self._dirty[key] = version
            self._dirty.move_to_end(key)
        self._prune_states()
        return key

    def _find_state_key_by_id(self, mongo_id: Any) -> tuple[int, str] | None:
        for key, row in self._approach.items():
            if row.get("_id") == mongo_id:
                return key
        return None

    def apply_approach_update(
        self,
        user_id: int,
        cycle_cache: dict[str, dict[str, Any]],
        query: dict[str, Any],
        update: dict[str, Any],
        *,
        upsert: bool = False,
    ) -> Any:
        """Apply lifecycle persistence to memory immediately and coalesce Mongo work."""
        icao = str(query.get("aircraft_icao24") or "").strip()
        key: tuple[int, str] | None = (int(user_id), icao) if icao else None
        if key is None and "_id" in query:
            key = self._find_state_key_by_id(query.get("_id"))
        if key is None:
            storage_metrics.record_drop(optional=False)
            logger.error("storage_v48_state_key_unresolved user=%s", user_id)
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)

        existing = self._approach.get(key) or cycle_cache.get(key[1])
        if existing is None and not upsert:
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)
        base = dict(existing or {"user_id": key[0], "aircraft_icao24": key[1]})
        document = _apply_mongo_style_update(base, update)
        document.setdefault("user_id", key[0])
        document.setdefault("aircraft_icao24", key[1])
        self._cache_state(document, dirty=True)
        cycle_cache[key[1]] = dict(document)
        return SimpleNamespace(
            matched_count=1 if existing is not None else 0,
            modified_count=1,
            upserted_id=None,
        )

    def _prune_states(self) -> None:
        now = datetime.now(timezone.utc)
        for key, row in list(self._approach.items()):
            expires = _aware_datetime(row.get("expires_at"))
            if not row.get("active") and expires is not None and expires <= now and key not in self._dirty:
                self._approach.pop(key, None)
                self._approach_versions.pop(key, None)

        while len(self._approach) > _APPROACH_CACHE_MAX:
            removable = next(
                (key for key, row in self._approach.items() if not row.get("active") and key not in self._dirty),
                None,
            )
            if removable is None:
                removable = next(
                    (key for key, row in self._approach.items() if not row.get("active")),
                    None,
                )
            if removable is None:
                removable = next(iter(self._approach))
                storage_metrics.record_drop(optional=False)
                logger.error("storage_v48_active_state_pressure limit=%d", _APPROACH_CACHE_MAX)
            elif removable in self._dirty:
                storage_metrics.record_drop(optional=False)
                logger.error("storage_v48_inactive_dirty_state_dropped key=%s", removable)
            self._approach.pop(removable, None)
            self._approach_versions.pop(removable, None)
            self._dirty.pop(removable, None)

        while len(self._dirty) > _APPROACH_DIRTY_MAX:
            # Prefer inactive lifecycle records under pathological outage pressure.
            dropped = next(
                (key for key in self._dirty if not (self._approach.get(key) or {}).get("active")),
                None,
            )
            if dropped is None:
                dropped = next(iter(self._dirty))
            self._dirty.pop(dropped, None)
            storage_metrics.record_drop(optional=False)
            logger.error("storage_v48_critical_queue_full dropped=%s limit=%d", dropped, _APPROACH_DIRTY_MAX)

    def enqueue_status_update(self, document_id: str, values: dict[str, Any]) -> None:
        key = str(document_id)
        version = self._status_versions.get(key, 0) + 1
        self._status_versions[key] = version
        merged = dict(self._status_pending.get(key, (0, {}))[1])
        merged.update(values)
        self._status_pending[key] = (version, merged)
        self._status_pending.move_to_end(key)
        while len(self._status_pending) > _STATUS_PENDING_MAX:
            self._status_pending.popitem(last=False)
            storage_metrics.record_drop(optional=True)

    def enqueue_optional_document(
        self,
        collection: str,
        document: dict[str, Any],
        *,
        document_id: str,
    ) -> bool:
        """Queue one idempotent analytical record for later Mongo persistence."""
        key = (str(collection), str(document_id))
        payload = dict(document)
        payload["_id"] = str(document_id)
        payload.setdefault("storage_queued_at", datetime.now(timezone.utc))
        if key not in self._optional_pending and len(self._optional_pending) >= _OPTIONAL_PENDING_MAX:
            self._optional_pending.popitem(last=False)
            storage_metrics.record_drop(optional=True)
            logger.warning("storage_v48_optional_queue_full limit=%d", _OPTIONAL_PENDING_MAX)
        self._optional_pending[key] = (time.monotonic(), payload)
        self._optional_pending.move_to_end(key)
        return True

    async def _persist_state(self, db: Any, key: tuple[int, str], version: int, document: dict[str, Any]) -> bool:
        clean = dict(document)
        clean.pop("_id", None)
        identity = {"user_id": key[0], "aircraft_icao24": key[1]}
        local_updated = _aware_datetime(clean.get("updated_at"))
        guarded = dict(identity)
        if local_updated is not None:
            guarded["$or"] = [
                {"updated_at": {"$lte": local_updated}},
                {"updated_at": {"$exists": False}},
            ]
        collection = db["approach_states"]
        result = await asyncio.wait_for(
            collection.replace_one(guarded, clean, upsert=False),
            timeout=_WRITE_TIMEOUT_S,
        )
        if int(getattr(result, "matched_count", 0) or 0) == 0:
            existing = await asyncio.wait_for(
                collection.find_one(identity), timeout=_WRITE_TIMEOUT_S
            )
            if existing is not None and _is_newer(existing.get("updated_at"), clean.get("updated_at")):
                if self._approach_versions.get(key) == version:
                    self._cache_state(existing, dirty=False)
                    self._dirty.pop(key, None)
                logger.warning("storage_v48_newer_persisted_state_preserved user=%s icao=%s", key[0], key[1])
                return True
            if existing is None:
                await asyncio.wait_for(
                    collection.replace_one(identity, clean, upsert=True),
                    timeout=_WRITE_TIMEOUT_S,
                )
            else:
                await asyncio.wait_for(
                    collection.replace_one(identity, clean, upsert=False),
                    timeout=_WRITE_TIMEOUT_S,
                )
        if self._approach_versions.get(key) == version:
            self._dirty.pop(key, None)
        return True

    async def _persist_status(self, db: Any, key: str, version: int, values: dict[str, Any]) -> None:
        await asyncio.wait_for(
            db["system_status"].update_one(
                {"_id": key}, {"$set": values}, upsert=True
            ),
            timeout=_WRITE_TIMEOUT_S,
        )
        current = self._status_pending.get(key)
        if current is not None and current[0] == version:
            self._status_pending.pop(key, None)

    async def _persist_optional(self, db: Any, key: tuple[str, str], queued_mono: float, document: dict[str, Any]) -> None:
        payload = dict(document)
        delay = max(0.0, time.monotonic() - queued_mono)
        payload["storage_persistence_state"] = "delayed" if delay >= 1.0 else "direct"
        payload["storage_deferred_seconds"] = round(delay, 3)
        await asyncio.wait_for(
            db[key[0]].replace_one({"_id": key[1]}, payload, upsert=True),
            timeout=_WRITE_TIMEOUT_S,
        )
        current = self._optional_pending.get(key)
        if current is not None and current[1].get("_id") == document.get("_id"):
            self._optional_pending.pop(key, None)

    async def flush_once(self, db: Any | None = None) -> int:
        """Persist bounded pending work in priority order; return completed count."""
        if not self._dirty and not self._status_pending and not self._optional_pending:
            return 0
        now_mono = time.monotonic()
        if now_mono < self._next_flush_attempt_mono:
            return 0
        try:
            db = self._db() if db is None else db
        except Exception:
            storage_metrics.set_state("degraded")
            self._flush_failures += 1
            storage_metrics.record_retry()
            self._next_flush_attempt_mono = now_mono + min(
                30.0, _FLUSH_FAILURE_BACKOFF_S * (2 ** min(self._flush_failures - 1, 3))
            )
            return 0

        completed = 0
        try:
            # User-critical alert lifecycle always drains before diagnostics.
            for key, version in list(self._dirty.items())[:16]:
                document = dict(self._approach.get(key) or {})
                if not document:
                    self._dirty.pop(key, None)
                    continue
                await self._persist_state(db, key, version, document)
                completed += 1

            for key, (version, values) in list(self._status_pending.items())[:8]:
                await self._persist_status(db, key, version, dict(values))
                completed += 1

            for key, (queued_mono, document) in list(self._optional_pending.items())[:16]:
                await self._persist_optional(db, key, queued_mono, dict(document))
                completed += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._flush_failures += 1
            storage_metrics.record_retry()
            self._next_flush_attempt_mono = time.monotonic() + min(
                30.0, _FLUSH_FAILURE_BACKOFF_S * (2 ** min(self._flush_failures - 1, 3))
            )
            storage_metrics.set_state("degraded")
            logger.warning(
                "storage_v48_flush_failed error=%s retry_in_s=%.1f",
                type(exc).__name__,
                max(0.0, self._next_flush_attempt_mono - time.monotonic()),
            )
            return completed

        self._flush_failures = 0
        self._next_flush_attempt_mono = 0.0
        if completed:
            storage_metrics.set_state("healthy")
        return completed

    async def _refresh_loop(self) -> None:
        while True:
            try:
                elapsed = time.monotonic() - self._last_config_refresh_mono
                if self._config_invalidated or elapsed >= _CONFIG_REFRESH_S:
                    await self.refresh_config_once()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("storage_v48_refresh_loop_failed")
                await asyncio.sleep(1.0)

    async def _flush_loop(self) -> None:
        while True:
            try:
                completed = await self.flush_once()
                if self._next_flush_attempt_mono:
                    delay = max(0.25, min(1.0, self._next_flush_attempt_mono - time.monotonic()))
                else:
                    delay = _FLUSH_IDLE_S if completed else 0.5
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("storage_v48_flush_loop_failed")
                await asyncio.sleep(_FLUSH_FAILURE_BACKOFF_S)

    def start(self) -> None:
        self._closed = False
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="storage-v48-config-refresh")
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop(), name="storage-v48-persistence")

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in (self._refresh_task, self._flush_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_task = None
        self._flush_task = None

    def snapshot(self) -> dict[str, Any]:
        config_age = (
            max(0.0, time.monotonic() - self._last_config_refresh_mono)
            if self._last_config_refresh_mono
            else None
        )
        retry_in = max(0.0, self._next_flush_attempt_mono - time.monotonic()) if self._next_flush_attempt_mono else 0.0
        return {
            "config_loaded": self._config_loaded,
            "monitoring_safe": self.monitoring_safe,
            "cached_users": len(self._users),
            "config_age_s": round(config_age, 1) if config_age is not None else None,
            "cached_encounters": len(self._approach),
            "critical_write_queue_depth": len(self._dirty),
            "status_write_queue_depth": len(self._status_pending),
            "optional_write_queue_depth": len(self._optional_pending),
            "flush_failures": self._flush_failures,
            "flush_retry_in_s": round(retry_in, 1),
        }


storage_runtime = StorageRuntimeV48()


def invalidate_user_config(user_id: int | None = None) -> None:
    storage_runtime.invalidate_user_config(user_id)
