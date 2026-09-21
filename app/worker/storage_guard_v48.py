"""Plane Alerts v4.8 storage isolation for the live aircraft cycle.

This module is intentionally installed after the established v4.2-v4.7 guards.
It changes only storage-facing hooks: verified configuration and approach state
come from the bounded in-process runtime, while persistence is coalesced in the
background. Prediction/CPA/qualification behavior is not modified.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any

from app.storage_runtime_v48 import storage_runtime
from app.version import COMMIT, VERSION
from app.worker import cadence_guard_v423 as cadence
from app.worker import monitor, v36

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL_MATERIALIZE_PROFILE = None
_ORIGINAL_LOAD_ACTIVE_USERS = None


def _memory_state_id(user_id: int, icao24: str) -> str:
    """Stable in-process identity for a lifecycle record not yet assigned Mongo _id."""
    return f"v48-memory:{int(user_id)}:{str(icao24)}"


def _normalize_state_query(
    user_id: int,
    cache: dict[str, dict[str, Any]],
    query: dict[str, Any],
) -> dict[str, Any]:
    if "_id" not in query or "aircraft_icao24" in query:
        return dict(query)
    wanted = query.get("_id")
    for icao24, document in cache.items():
        if document.get("_id") == wanted:
            return {"user_id": int(user_id), "aircraft_icao24": str(icao24)}
    return dict(query)


async def _load_active_users_coherent(db: Any) -> dict[int, dict[str, Any]]:
    """Load only coherent config generations and preserve per-user LKG on tears.

    A failed second materialization write must not turn a successful refresh into
    removal of a previously monitored user. On a cold process, a torn legacy
    materialization is repaired from the authoritative active profile before the
    user is admitted to the cache.
    """
    controls: dict[int, dict[str, Any]] = {}
    active_profile_ids: dict[int, str] = {}
    ids: list[int] = []
    cursor = db["users"].find(
        {"setup_complete": True},
        {"user_id": 1, "admin_controls": 1, "active_profile_id": 1},
    )
    async for row in cursor:
        uid = int(row["user_id"])
        ids.append(uid)
        controls[uid] = dict(row.get("admin_controls") or {})
        active_profile_ids[uid] = str(row.get("active_profile_id") or "")
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
        coherent = bool(
            loc
            and prefs
            and loc.get("config_revision")
            and loc.get("config_revision") == prefs.get("config_revision")
        )
        if coherent:
            out[uid] = {
                "user_id": uid,
                "location": loc,
                "preferences": prefs,
                "admin_control": controls.get(uid, {}),
            }
            continue

        previous = storage_runtime._users.get(uid)  # noqa: SLF001 - same-runtime safety guard
        if previous is not None:
            preserved = deepcopy(previous)
            preserved["admin_control"] = controls.get(uid, {})
            out[uid] = preserved
            logger.warning(
                "storage_v48_config_generation_incomplete user=%s action=preserve_lkg",
                uid,
            )
            continue

        profile_id = active_profile_ids.get(uid, "")
        if not profile_id:
            logger.warning(
                "storage_v48_config_generation_incomplete user=%s action=cold_skip reason=no_active_profile",
                uid,
            )
            continue

        try:
            profile = await db["profiles"].find_one({"user_id": uid, "profile_id": profile_id})
            if not profile:
                logger.warning(
                    "storage_v48_config_generation_incomplete user=%s action=cold_skip reason=profile_missing",
                    uid,
                )
                continue
            from app import alert_profiles

            await alert_profiles.materialize_profile(profile)
            repaired_loc = await db["locations"].find_one({"user_id": uid})
            repaired_prefs = await db["preferences"].find_one({"user_id": uid})
            if (
                repaired_loc
                and repaired_prefs
                and repaired_loc.get("config_revision")
                and repaired_loc.get("config_revision") == repaired_prefs.get("config_revision")
            ):
                out[uid] = {
                    "user_id": uid,
                    "location": repaired_loc,
                    "preferences": repaired_prefs,
                    "admin_control": controls.get(uid, {}),
                }
                logger.warning(
                    "storage_v48_config_generation_repaired user=%s profile=%s",
                    uid,
                    profile_id,
                )
            else:
                logger.error("storage_v48_config_generation_repair_unverified user=%s", uid)
        except Exception as exc:
            logger.warning(
                "storage_v48_config_generation_repair_failed user=%s error=%s",
                uid,
                type(exc).__name__,
            )
    return out


def _queue_observer_elevation_cached(uid: int, loc: dict[str, Any]) -> None:
    """Queue at most one terrain lookup per exact persisted location generation."""
    if loc.get("elevation_m") is not None:
        return
    try:
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return
    revision = str(loc.get("config_revision") or "legacy")
    monitor.enrichment.get(
        (
            "observer_elevation_v542",
            int(uid),
            revision,
            round(lat, 6),
            round(lon, 6),
        ),
        lambda: monitor.resolve_observer_elevation(int(uid), lat, lon),
        ttl=900,
    )


async def _get_active_users_cached() -> list[dict[str, Any]]:
    """Return last-known-good config; only the first cold cycle may warm Mongo."""
    if not storage_runtime.config_loaded:
        try:
            await storage_runtime.warm()
        except Exception as exc:
            logger.warning("storage_v48_cold_warm_failed error=%s", type(exc).__name__)
            return []
    storage_runtime.start()
    users = storage_runtime.active_users()
    for user in users:
        try:
            _queue_observer_elevation_cached(int(user["user_id"]), user.get("location") or {})
        except Exception:
            logger.debug("observer_elevation_queue_failed", exc_info=True)
    return users


async def _attach_admin_controls_cached(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for user in users:
        user.setdefault("admin_control", {})
    return users


async def _prefetch_approach_states_cached(
    user_id: int,
    aircraft_list: list[Any],
) -> dict[str, dict[str, Any]]:
    icao24s = {
        str(getattr(aircraft, "icao24", "") or "").strip()
        for aircraft in aircraft_list
        if str(getattr(aircraft, "icao24", "") or "").strip()
    }
    states = storage_runtime.approach_states(int(user_id), icao24s)
    for icao24, document in states.items():
        document.setdefault("_id", _memory_state_id(int(user_id), icao24))
    return states


async def _approach_update_cached(
    self: cadence._ApproachStatesCollectionProxy,
    query: dict[str, Any],
    update: dict[str, Any],
    *,
    upsert: bool = False,
    **_kwargs: Any,
) -> Any:
    normalized = _normalize_state_query(int(self._user_id), self._cache, query)
    return storage_runtime.apply_approach_update(
        int(self._user_id),
        self._cache,
        normalized,
        update,
        upsert=bool(upsert),
    )


async def _record_worker_heartbeat_cached(active_count: int, notifications_sent: int) -> None:
    storage_runtime.enqueue_status_update(
        "monitor_worker",
        {
            "last_cycle_time": monitor._last_cycle_time,
            "last_cycle_duration_ms": round(monitor._last_cycle_duration * 1000, 1),
            "total_cycles": monitor._total_cycles,
            "active_users": int(active_count),
            "notifications_sent_last_cycle": int(notifications_sent),
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _record_v36_metrics_cached(
    *,
    region_count: int,
    provider_queries: int,
    cache_hits: int,
    priority_users: int,
    deferred_users: int,
    notifications_paused: int,
) -> None:
    storage_runtime.enqueue_status_update(
        "monitor_worker",
        {
            "plane_version": VERSION,
            "plane_commit": COMMIT,
            "shared_regions_last_cycle": int(region_count),
            "provider_queries_last_cycle": int(provider_queries),
            "shared_snapshot_cache_hits_last_cycle": int(cache_hits),
            "priority_users_last_cycle": int(priority_users),
            "deferred_users_last_cycle": int(deferred_users),
            "notifications_paused_last_cycle": int(notifications_paused),
            "polling_mode": "adaptive-shared-regions-priority-storage-isolated",
            "updated_at": datetime.now(timezone.utc),
        },
    )


async def _materialize_profile_invalidating(profile: dict[str, Any]) -> None:
    """Invalidate the cached config only after the persisted write succeeded."""
    assert _ORIGINAL_MATERIALIZE_PROFILE is not None
    await _ORIGINAL_MATERIALIZE_PROFILE(profile)
    try:
        storage_runtime.invalidate_user_config(int(profile["user_id"]))
    except (KeyError, TypeError, ValueError):
        storage_runtime.invalidate_user_config()


def install_storage_guard_v48() -> None:
    global _INSTALLED, _ORIGINAL_MATERIALIZE_PROFILE, _ORIGINAL_LOAD_ACTIVE_USERS
    if _INSTALLED:
        return

    _ORIGINAL_LOAD_ACTIVE_USERS = storage_runtime._load_active_users  # noqa: SLF001
    storage_runtime._load_active_users = _load_active_users_coherent  # type: ignore[method-assign]

    monitor._get_active_users = _get_active_users_cached
    cadence._prefetch_approach_states = _prefetch_approach_states_cached
    cadence._ApproachStatesCollectionProxy.update_one = _approach_update_cached
    v36._attach_admin_controls = _attach_admin_controls_cached
    monitor._record_worker_heartbeat = _record_worker_heartbeat_cached
    v36._record_v36_metrics = _record_v36_metrics_cached

    try:
        from app import alert_profiles

        _ORIGINAL_MATERIALIZE_PROFILE = alert_profiles.materialize_profile
        alert_profiles.materialize_profile = _materialize_profile_invalidating
    except Exception:
        logger.exception("storage_v48_profile_invalidation_install_failed")

    _INSTALLED = True
    logger.info(
        "Plane Alerts v4.8 storage guard enabled: config=memory state=memory persistence=bounded-background"
    )
