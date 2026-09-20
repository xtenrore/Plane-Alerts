"""Plane Alerts v4.4 priority-aware shared ADS-B scheduling.

v4.4 keeps the proven shared-provider polling architecture while adding alert
profiles and inherited aircraft filtering outside provider I/O. Priority users
are still evaluated first and their shared region stays on the five-second hot
cadence. Delay Time remains a minimum per-user evaluation delay, so
administrators can slow individual users without multiplying provider calls.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.agy_state import is_agy_console_active
from app.config import settings
from app.database import system_status_col, users_col
from app.version import COMMIT, VERSION
from app.worker import monitor, v35

logger = logging.getLogger(__name__)
_last_user_processed_mono: dict[int, float] = {}


def _control(user: dict[str, Any]) -> dict[str, Any]:
    raw = user.get("admin_control") or {}
    # Opening the private /agy console temporarily mutes aircraft notifications
    # without changing the user's saved notification preference. The in-memory
    # flag automatically clears on /agy stop, console exit, or bot restart.
    agy_muted = is_agy_console_active(int(user.get("user_id", 0) or 0))
    return {
        "priority_enabled": bool(raw.get("priority_enabled", False)),
        "delay_seconds": max(5.0, min(120.0, float(raw.get("delay_seconds", settings.poll_interval_seconds)))),
        "notifications_enabled": bool(raw.get("notifications_enabled", True)) and not agy_muted,
    }


def _priority(user: dict[str, Any]) -> bool:
    return bool(_control(user)["priority_enabled"])


def _is_due(user: dict[str, Any], now_mono: float) -> bool:
    uid = int(user["user_id"])
    previous = _last_user_processed_mono.get(uid)
    if previous is None:
        return True
    return (now_mono - previous) >= float(_control(user)["delay_seconds"])


def _user_order(user: dict[str, Any]) -> tuple[int, float, str]:
    control = _control(user)
    return (
        0 if control["priority_enabled"] else 1,
        float(control["delay_seconds"]),
        str(user["user_id"]),
    )


async def _attach_admin_controls(users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not users:
        return users
    ids = [int(user["user_id"]) for user in users]
    controls: dict[int, dict[str, Any]] = {}
    cursor = users_col().find(
        {"user_id": {"$in": ids}},
        {"user_id": 1, "admin_controls": 1},
    )
    async for doc in cursor:
        controls[int(doc["user_id"])] = doc.get("admin_controls") or {}
    for user in users:
        user["admin_control"] = controls.get(int(user["user_id"]), {})
    return users


def _region_order(region: v35.SharedPollRegion) -> tuple[int, float, str]:
    priority = any(_priority(user) for user in region.users)
    fastest = min(float(_control(user)["delay_seconds"]) for user in region.users)
    return (0 if priority else 1, fastest, region.key)


def _promote_priority_region(region_key: str, now_mono: float) -> None:
    """Keep a priority region at the hot five-second provider cadence."""
    snapshot = v35._shared_poller._snapshots.get(region_key)
    if snapshot is not None:
        snapshot.hot_until_mono = max(snapshot.hot_until_mono, now_mono + v35.HOT_HOLD_S)


def _record_users_processed(users: list[dict[str, Any]], evaluation_started_mono: float) -> None:
    """Anchor delay to evaluation start, not completion.

    Provider/network latency is work time, not extra cooldown. If a supposedly
    five-second evaluation takes nine seconds, the next cycle should be eligible
    immediately rather than waiting another five seconds after completion.
    """
    for user in users:
        _last_user_processed_mono[int(user["user_id"])] = evaluation_started_mono


def _collect_due_users(
    region: v35.SharedPollRegion,
    now_mono: float,
) -> tuple[list[dict[str, Any]], int]:
    """Resolve due users using a timestamp sampled for this specific region.

    A previous region may spend several seconds waiting on a public ADS-B feed.
    Reusing a cycle-start timestamp would incorrectly defer users in later
    regions even though their delay elapsed while the earlier region was busy.
    """
    due_users: list[dict[str, Any]] = []
    deferred = 0
    for user in region.users:
        control = _control(user)
        if not control["notifications_enabled"]:
            continue
        if _is_due(user, now_mono):
            due_users.append(user)
        else:
            deferred += 1
    return due_users, deferred


async def _record_v36_metrics(
    *,
    region_count: int,
    provider_queries: int,
    cache_hits: int,
    priority_users: int,
    deferred_users: int,
    notifications_paused: int,
) -> None:
    try:
        await system_status_col().update_one(
            {"_id": "monitor_worker"},
            {"$set": {
                "plane_version": VERSION,
                "plane_commit": COMMIT,
                "shared_regions_last_cycle": region_count,
                "provider_queries_last_cycle": provider_queries,
                "shared_snapshot_cache_hits_last_cycle": cache_hits,
                "priority_users_last_cycle": priority_users,
                "deferred_users_last_cycle": deferred_users,
                "notifications_paused_last_cycle": notifications_paused,
                "polling_mode": "adaptive-shared-regions-priority",
            }},
            upsert=True,
        )
    except Exception:
        logger.debug("Unable to persist v4.4 polling metrics", exc_info=True)


async def _monitor_cycle_v36() -> None:
    start = time.time()
    users = await monitor._get_active_users()
    users = await _attach_admin_controls(users)
    if not users:
        monitor._last_cycle_time = time.time()
        monitor._last_cycle_duration = time.time() - start
        monitor._total_cycles += 1
        await monitor._record_worker_heartbeat(0, 0)
        await _record_v36_metrics(
            region_count=0,
            provider_queries=0,
            cache_hits=0,
            priority_users=0,
            deferred_users=0,
            notifications_paused=0,
        )
        return

    live_ids = {int(user["user_id"]) for user in users}
    for uid in list(_last_user_processed_mono):
        if uid not in live_ids:
            _last_user_processed_mono.pop(uid, None)

    regions = v35.build_shared_regions(users)
    regions.sort(key=_region_order)
    v35._shared_poller.prune({region.key for region in regions})

    notifications = 0
    provider_queries = 0
    cache_hits = 0
    priority_users = sum(1 for user in users if _priority(user))
    paused_users = sum(1 for user in users if not _control(user)["notifications_enabled"])
    deferred_users = 0
    processed_users = 0
    cycle_number = monitor._total_cycles
    stagger = len(regions) > 1

    for region in regions:
        # Important: sample monotonic time per region. A slow earlier provider
        # must not make a later region use a stale due/defer decision.
        region_now_mono = time.monotonic()
        due_users, region_deferred = _collect_due_users(region, region_now_mono)
        deferred_users += region_deferred

        if not due_users:
            continue

        due_users.sort(key=_user_order)
        region_has_priority = any(_priority(user) for user in due_users)
        if region_has_priority:
            _promote_priority_region(region.key, region_now_mono)

        active_region = v35.SharedPollRegion(
            key=region.key,
            users=due_users,
            latitude=region.latitude,
            longitude=region.longitude,
            radius_nm=region.radius_nm,
        )

        evaluation_started_mono = region_now_mono
        try:
            sent, poll = await v35._process_shared_region(
                active_region,
                cycle_number=cycle_number,
                stagger=stagger and not region_has_priority,
            )
            if region_has_priority:
                _promote_priority_region(region.key, time.monotonic())
            notifications += sent
            provider_queries += poll.provider_queries
            cache_hits += int(poll.cache_hit)
            processed_users += len(due_users)
            _record_users_processed(due_users, evaluation_started_mono)
        except Exception:
            logger.exception(
                "v4_shared_region_failed region=%s users=%d priority=%s",
                region.key,
                len(due_users),
                region_has_priority,
            )

    monitor._history.prune()
    monitor._last_cycle_time = time.time()
    monitor._last_cycle_duration = time.time() - start
    monitor._total_cycles += 1
    await monitor._record_worker_heartbeat(len(users), notifications)
    await _record_v36_metrics(
        region_count=len(regions),
        provider_queries=provider_queries,
        cache_hits=cache_hits,
        priority_users=priority_users,
        deferred_users=deferred_users,
        notifications_paused=paused_users,
    )
    logger.info(
        "v4_cycle users=%d processed=%d priority=%d deferred=%d paused=%d regions=%d provider_queries=%d cache_hits=%d notifications=%d duration_ms=%.1f",
        len(users),
        processed_users,
        priority_users,
        deferred_users,
        paused_users,
        len(regions),
        provider_queries,
        cache_hits,
        notifications,
        monitor._last_cycle_duration * 1000.0,
    )


async def run_monitor_cycle_v36() -> None:
    try:
        await _monitor_cycle_v36()
    except Exception:
        logger.exception("Plane Alerts v4.4 monitor cycle failed unexpectedly")