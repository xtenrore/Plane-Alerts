"""Plane? v3.5 shared adaptive ADS-B polling.

v3.5 removes provider traffic from the number of users. Nearby users are packed
into the largest safe shared query regions, each region reuses one aircraft
snapshot, providers are rotated rather than all queried every cycle, and quiet
regions poll more slowly than regions with an aircraft approaching a user.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.database import system_status_col
from app.intelligence.route_history import route_history_service
from app.worker.geo import bounding_box, haversine, km_to_nautical_miles, merge_bounding_boxes
from app.worker import monitor

logger = logging.getLogger(__name__)

MAX_PROVIDER_RADIUS_NM = 250
DISCOVERY_INTERVAL_S = 15.0
HOT_INTERVAL_S = 5.0
HOT_HOLD_S = 60.0
HOT_MARGIN_KM = 50.0
CONTINUITY_TTL_S = 28.0
OPEN_SKY_FALLBACK_TIMEOUT_S = 2.5
PUBLIC_PROVIDERS = ("adsb.lol", "adsb.fi", "airplanes.live", "adsb.one")


@dataclass(slots=True)
class SharedPollRegion:
    key: str
    users: list[dict]
    latitude: float
    longitude: float
    radius_nm: int


@dataclass(slots=True)
class SharedSnapshot:
    fetched_mono: float
    fetched_wall: float
    aircraft: list[Any]
    by_provider: dict[str, list[Any]]
    hot_until_mono: float
    provider_name: str


@dataclass(slots=True)
class PollResult:
    aircraft: list[Any]
    by_provider: dict[str, list[Any]]
    fresh: bool
    cache_hit: bool
    provider_queries: int
    provider_name: str
    cache_age_s: float


def _coverage_box(user: dict) -> tuple[float, float, float, float]:
    loc = user["location"]
    radius = float(loc.get("radius_km", settings.default_radius_km)) + 120.0
    return bounding_box(float(loc["latitude"]), float(loc["longitude"]), radius)


def _geometry(users: list[dict]) -> tuple[float, float, int]:
    merged = merge_bounding_boxes([_coverage_box(user) for user in users])
    clat = (merged[0] + merged[1]) / 2.0
    clon = (merged[2] + merged[3]) / 2.0
    diagonal_km = haversine(merged[0], merged[2], merged[1], merged[3])
    radius_nm = max(70, int(km_to_nautical_miles(diagonal_km / 2.0) + 15.0))
    return clat, clon, radius_nm


def _region_key(users: list[dict]) -> str:
    # Include coarse location/radius in the fingerprint. A saved-location move
    # must immediately create a new cache key rather than briefly reusing the
    # previous geographic snapshot for the same user IDs.
    members: list[str] = []
    for user in sorted(users, key=lambda u: str(u["user_id"])):
        loc = user["location"]
        members.append(
            f"{user['user_id']}:{float(loc['latitude']):.3f}:{float(loc['longitude']):.3f}:"
            f"{float(loc.get('radius_km', settings.default_radius_km)):.1f}"
        )
    digest = hashlib.sha1("|".join(members).encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"shared-{digest}"


def build_shared_regions(users: list[dict]) -> list[SharedPollRegion]:
    """Greedily pack users into safe <=250 NM provider queries.

    This deliberately ignores geohash cell boundaries. Two users a few hundred
    metres apart but on opposite sides of a geohash border should share a poll.
    """
    groups: list[list[dict]] = []
    ordered = sorted(
        users,
        key=lambda user: (
            float(user["location"]["latitude"]),
            float(user["location"]["longitude"]),
            str(user["user_id"]),
        ),
    )
    for user in ordered:
        best_index: int | None = None
        best_radius: int | None = None
        for index, group in enumerate(groups):
            candidate = [*group, user]
            _, _, radius_nm = _geometry(candidate)
            if radius_nm <= MAX_PROVIDER_RADIUS_NM and (best_radius is None or radius_nm < best_radius):
                best_index = index
                best_radius = radius_nm
        if best_index is None:
            groups.append([user])
        else:
            groups[best_index].append(user)

    regions: list[SharedPollRegion] = []
    for group in groups:
        clat, clon, radius_nm = _geometry(group)
        regions.append(
            SharedPollRegion(
                key=_region_key(group),
                users=group,
                latitude=clat,
                longitude=clon,
                radius_nm=min(MAX_PROVIDER_RADIUS_NM, radius_nm),
            )
        )
    return regions


def _aged_aircraft(aircraft: list[Any], elapsed_s: float) -> list[Any]:
    if elapsed_s <= 0.05:
        return list(aircraft)
    aged: list[Any] = []
    for ac in aircraft:
        try:
            previous_age = float(getattr(ac, "position_age_s", 0.0) or 0.0)
            aged.append(
                ac.model_copy(
                    update={
                        "position_age_s": previous_age + elapsed_s,
                        "data_quality": "shared-cache",
                    }
                )
            )
        except Exception:
            aged.append(ac)
    return aged


def _region_is_hot(region: SharedPollRegion, aircraft: list[Any]) -> bool:
    for user in region.users:
        loc = user["location"]
        threshold = float(loc.get("radius_km", settings.default_radius_km)) + HOT_MARGIN_KM
        ulat = float(loc["latitude"])
        ulon = float(loc["longitude"])
        for ac in aircraft:
            if getattr(ac, "latitude", None) is None or getattr(ac, "longitude", None) is None:
                continue
            if haversine(ulat, ulon, float(ac.latitude), float(ac.longitude)) <= threshold:
                return True
    return False


class SharedRegionPoller:
    def __init__(self) -> None:
        self._snapshots: dict[str, SharedSnapshot] = {}
        self._provider_cursor: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def prune(self, live_keys: set[str]) -> None:
        for key in set(self._snapshots) | set(self._provider_cursor) | set(self._locks):
            if key not in live_keys:
                self._snapshots.pop(key, None)
                self._provider_cursor.pop(key, None)
                self._locks.pop(key, None)

    def _next_public_provider(self, key: str) -> str:
        cursor = self._provider_cursor.get(key)
        if cursor is None:
            cursor = int(hashlib.sha1(key.encode("utf-8"), usedforsecurity=False).hexdigest()[:4], 16)
        name = PUBLIC_PROVIDERS[cursor % len(PUBLIC_PROVIDERS)]
        self._provider_cursor[key] = cursor + 1
        return name

    async def _query_one(self, region: SharedPollRegion, provider_name: str):
        return await monitor._provider_manager.query_providers(
            latitude=region.latitude,
            longitude=region.longitude,
            radius_nm=region.radius_nm,
            provider_names=[provider_name],
        )

    async def poll(
        self,
        region: SharedPollRegion,
        *,
        cycle_number: int,
        stagger_new_regions: bool,
    ) -> PollResult:
        lock = self._locks.setdefault(region.key, asyncio.Lock())
        async with lock:
            now_mono = time.monotonic()
            now_wall = time.time()
            previous = self._snapshots.get(region.key)

            if previous is not None:
                interval = HOT_INTERVAL_S if previous.hot_until_mono > now_mono else DISCOVERY_INTERVAL_S
                age = max(0.0, now_mono - previous.fetched_mono)
                if age < interval:
                    return PollResult(
                        aircraft=_aged_aircraft(previous.aircraft, age),
                        by_provider=previous.by_provider,
                        fresh=False,
                        cache_hit=True,
                        provider_queries=0,
                        provider_name=previous.provider_name,
                        cache_age_s=age,
                    )
            elif stagger_new_regions:
                slot = int(hashlib.sha1(region.key.encode("utf-8"), usedforsecurity=False).hexdigest()[-2:], 16) % 3
                if cycle_number % 3 != slot:
                    return PollResult([], {}, False, True, 0, "staggered", 0.0)

            attempts: list[str] = []
            aircraft: list[Any] = []
            by_provider: dict[str, list[Any]] = {}
            selected = ""

            # Normal cycles query one public feed. A second public feed is used
            # only if the first is blank/failing, avoiding 4x provider traffic.
            for _ in range(2):
                provider_name = self._next_public_provider(region.key)
                if provider_name in attempts:
                    continue
                attempts.append(provider_name)
                result, provider_result = await self._query_one(region, provider_name)
                by_provider.update(provider_result)
                if result:
                    aircraft = result
                    selected = provider_name
                    break

            # Authenticated OpenSky is a last hedge, not a parallel every-cycle
            # request. This protects free/public feeds and preserves key quota.
            if not aircraft and monitor._provider_manager.opensky.can_request_now():
                attempts.append("opensky")
                try:
                    result, provider_result = await asyncio.wait_for(
                        self._query_one(region, "opensky"),
                        timeout=OPEN_SKY_FALLBACK_TIMEOUT_S,
                    )
                    by_provider.update(provider_result)
                    if result:
                        aircraft = result
                        selected = "opensky"
                except asyncio.TimeoutError:
                    logger.warning("v35_opensky_fallback_timeout region=%s", region.key)
                except Exception:
                    logger.exception("v35_opensky_fallback_failed region=%s", region.key)

            if aircraft:
                hot_until = now_mono + HOT_HOLD_S if _region_is_hot(region, aircraft) else now_mono
                snapshot = SharedSnapshot(
                    fetched_mono=now_mono,
                    fetched_wall=now_wall,
                    aircraft=list(aircraft),
                    by_provider=by_provider,
                    hot_until_mono=hot_until,
                    provider_name=selected or (attempts[-1] if attempts else "unknown"),
                )
                self._snapshots[region.key] = snapshot
                return PollResult(
                    aircraft=list(aircraft),
                    by_provider=by_provider,
                    fresh=True,
                    cache_hit=False,
                    provider_queries=len(attempts),
                    provider_name=snapshot.provider_name,
                    cache_age_s=0.0,
                )

            # Never turn one transient blank response into an empty sky. Reuse a
            # prior non-empty region snapshot briefly; position ages continue
            # increasing, so the existing 30s stale guard stays authoritative.
            if previous is not None and previous.aircraft:
                age = max(0.0, now_mono - previous.fetched_mono)
                if age <= CONTINUITY_TTL_S:
                    logger.warning(
                        "v35_region_continuity region=%s age=%.1f provider_attempts=%s",
                        region.key,
                        age,
                        ",".join(attempts),
                    )
                    return PollResult(
                        aircraft=_aged_aircraft(previous.aircraft, age),
                        by_provider=previous.by_provider,
                        fresh=False,
                        cache_hit=True,
                        provider_queries=len(attempts),
                        provider_name="continuity",
                        cache_age_s=age,
                    )

            # A genuinely empty region is also a cacheable result. Without this,
            # sparse/empty skies would retry two public providers (+ OpenSky)
            # every five-second scheduler tick and waste more quota than busy
            # regions. Cache emptiness at the 15-second discovery cadence.
            empty_snapshot = SharedSnapshot(
                fetched_mono=now_mono,
                fetched_wall=now_wall,
                aircraft=[],
                by_provider=by_provider,
                hot_until_mono=now_mono,
                provider_name=selected or "empty",
            )
            self._snapshots[region.key] = empty_snapshot
            return PollResult([], by_provider, True, False, len(attempts), empty_snapshot.provider_name, 0.0)


_shared_poller = SharedRegionPoller()


async def _process_shared_region(region: SharedPollRegion, *, cycle_number: int, stagger: bool) -> tuple[int, PollResult]:
    poll = await _shared_poller.poll(region, cycle_number=cycle_number, stagger_new_regions=stagger)
    if not poll.aircraft:
        return 0, poll

    now = time.time()
    accepted_aircraft: list[Any] = []
    for ac in poll.aircraft:
        if not getattr(ac, "has_position", False):
            continue
        sample = monitor._sample(ac, now)
        before = monitor._history.get(ac.icao24)
        history = monitor._history.add(ac.icao24, sample)
        if monitor._accepted_latest(sample, history):
            accepted_aircraft.append(ac)
        else:
            previous = before[-1] if before else None
            jump = haversine(previous.latitude, previous.longitude, sample.latitude, sample.longitude) if previous else -1.0
            logger.warning(
                "adsb_outlier_rejected icao=%s jump_km=%.2f sample_age=%.1f",
                ac.icao24,
                jump,
                sample.position_age_s,
            )

    if accepted_aircraft and poll.fresh:
        observations = await asyncio.gather(
            *(route_history_service.observe(ac, now=now) for ac in accepted_aircraft),
            return_exceptions=True,
        )
        failures = sum(isinstance(item, Exception) for item in observations)
        if failures:
            logger.warning("flight_route_observation_failures count=%d", failures)

    # Provider learning is recorded only for real provider polls, never every
    # time a shared cached snapshot is reused by the 5-second monitor loop.
    if poll.fresh and poll.by_provider:
        for user in region.users:
            loc = user["location"]
            await monitor.provider_learner.record_cycle_observation(
                user_id=user["user_id"],
                geohash=str(loc.get("geohash") or region.key),
                results_by_provider=poll.by_provider,
                user_lat=loc["latitude"],
                user_lon=loc["longitude"],
                radius_km=loc.get("radius_km", settings.default_radius_km),
            )

    count = 0
    for user in region.users:
        count += await monitor._match_user_aircraft(user, accepted_aircraft, poll.by_provider)
    return count, poll


async def _record_v35_metrics(region_count: int, provider_queries: int, cache_hits: int) -> None:
    try:
        await system_status_col().update_one(
            {"_id": "monitor_worker"},
            {"$set": {
                "plane_version": "3.5.0",
                "shared_regions_last_cycle": region_count,
                "provider_queries_last_cycle": provider_queries,
                "shared_snapshot_cache_hits_last_cycle": cache_hits,
                "polling_mode": "adaptive-shared-regions",
            }},
            upsert=True,
        )
    except Exception:
        logger.debug("Unable to persist v3.5 polling metrics", exc_info=True)


async def _monitor_cycle_v35() -> None:
    start = time.time()
    users = await monitor._get_active_users()
    if not users:
        monitor._last_cycle_time = time.time()
        monitor._last_cycle_duration = time.time() - start
        monitor._total_cycles += 1
        await monitor._record_worker_heartbeat(0, 0)
        await _record_v35_metrics(0, 0, 0)
        return

    regions = build_shared_regions(users)
    _shared_poller.prune({region.key for region in regions})
    notifications = 0
    provider_queries = 0
    cache_hits = 0
    stagger = len(regions) > 1
    cycle_number = monitor._total_cycles

    # Intentionally sequential: separate world regions never stampede the same
    # provider simultaneously from this worker.
    for region in regions:
        try:
            sent, poll = await _process_shared_region(region, cycle_number=cycle_number, stagger=stagger)
            notifications += sent
            provider_queries += poll.provider_queries
            cache_hits += int(poll.cache_hit)
        except Exception:
            logger.exception("v35_shared_region_failed region=%s users=%d", region.key, len(region.users))

    monitor._history.prune()
    monitor._last_cycle_time = time.time()
    monitor._last_cycle_duration = time.time() - start
    monitor._total_cycles += 1
    await monitor._record_worker_heartbeat(len(users), notifications)
    await _record_v35_metrics(len(regions), provider_queries, cache_hits)
    logger.info(
        "v35_cycle users=%d shared_regions=%d provider_queries=%d cache_hits=%d notifications=%d duration_ms=%.1f",
        len(users),
        len(regions),
        provider_queries,
        cache_hits,
        notifications,
        monitor._last_cycle_duration * 1000.0,
    )


async def run_monitor_cycle_v35() -> None:
    try:
        await _monitor_cycle_v35()
    except Exception:
        logger.exception("Plane? v3.5 monitor cycle failed unexpectedly")