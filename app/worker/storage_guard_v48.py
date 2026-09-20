"""Plane Alerts v4.8 storage isolation for the live aircraft cycle.

This module is intentionally installed after the established v4.2-v4.7 guards.
It changes only storage-facing hooks: verified configuration and approach state
come from the bounded in-process runtime, while persistence is coalesced in the
background. Prediction/CPA/qualification behavior is not modified.
"""
from __future__ import annotations

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


async def _get_active_users_cached() -> list[dict[str, Any]]:
    """Return last-known-good config; only the first cold cycle may warm Mongo."""
    if not storage_runtime.config_loaded:
        try:
            await storage_runtime.warm()
        except Exception as exc:
            logger.warning("storage_v48_cold_warm_failed error=%s", type(exc).__name__)
            return []
    storage_runtime.start()
    return storage_runtime.active_users()


async def _attach_admin_controls_cached(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # v4.8 loads admin controls atomically with the user configuration snapshot.
    # Keep v36's field name and semantics so priority/delay behavior is unchanged.
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
    return storage_runtime.approach_states(int(user_id), icao24s)


async def _approach_update_cached(
    self: cadence._ApproachStatesCollectionProxy,
    query: dict[str, Any],
    update: dict[str, Any],
    *,
    upsert: bool = False,
    **_kwargs: Any,
) -> Any:
    """Commit lifecycle state to memory immediately; Mongo flush is background."""
    return storage_runtime.apply_approach_update(
        int(self._user_id),
        self._cache,
        query,
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
    global _INSTALLED, _ORIGINAL_MATERIALIZE_PROFILE
    if _INSTALLED:
        return

    # Replace v4.2.3's still-synchronous DB snapshots with the v4.8 memory copy.
    monitor._get_active_users = _get_active_users_cached
    cadence._prefetch_approach_states = _prefetch_approach_states_cached
    cadence._ApproachStatesCollectionProxy.update_one = _approach_update_cached

    # Admin controls are already part of the same verified configuration image.
    v36._attach_admin_controls = _attach_admin_controls_cached

    # Heartbeats/diagnostics are useful but must never stretch the alert cycle.
    monitor._record_worker_heartbeat = _record_worker_heartbeat_cached
    v36._record_v36_metrics = _record_v36_metrics_cached

    # Active profile materialization remains an acknowledged Mongo write. Only
    # after it succeeds do we invalidate the LKG cache and accelerate refresh.
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
