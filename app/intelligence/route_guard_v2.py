"""Latency-safe destination guard for live Plane Alerts.

This layer keeps route intelligence useful without allowing route/history I/O to
block the aircraft-alert loop. It also catches arrival turns earlier than the
legacy terminal-only rule.

Design rules:
* route and history data may veto a live CPA but never create one;
* network route resolution always runs in the background;
* first predictive alerts may be held only for one short bounded grace window;
* a fresh aircraft physically inside the user's radius is never hidden;
* route-history writes are background work, not notification-path work.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Iterable

from app.config import settings
from app.intelligence import route_guard as legacy
from app.intelligence import route_history as route_mod
from app.intelligence.trajectory import bearing_deg, haversine_km

logger = logging.getLogger(__name__)

_NEGATIVE_CACHE_S = 20.0
_HISTORY_CACHE_S = 120.0
_INITIAL_ROUTE_GRACE_S = 6.0
_PRETERMINAL_DESTINATION_KM = 240.0
_PRETERMINAL_MIN_CPA_S = 55.0
_ROUTE_REFRESH_CONCURRENCY = 4
_OBSERVE_TASK_LIMIT = 24
_INSTALLED = False
_ORIGINAL_OBSERVE = route_mod.RouteHistoryService.observe


def _angle_delta(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _choose_route_prefer_position_aware(
    callsign: str,
    candidates: list[tuple[str, route_mod.FlightRouteInfo | None]],
) -> tuple[route_mod.FlightRouteInfo | None, str]:
    """Keep the ordered position-aware source instead of discarding all data on conflict."""
    usable = [(source, route) for source, route in candidates if route is not None]
    if not usable:
        return None, ""
    plausible = [(source, route) for source, route in usable if route.plausible]
    pool = plausible or usable
    destinations = {
        (route.destination.code if route.destination else "").upper()
        for _, route in plausible
        if route.destination and route.destination.code
    }
    if len(destinations) > 1:
        logger.warning(
            "route_guard_source_conflict callsign=%s destinations=%s using=%s",
            callsign,
            ",".join(sorted(destinations)),
            pool[0][0],
        )
        return pool[0][1], f"{pool[0][0]}:conflict"
    return pool[0][1], pool[0][0]


def _cached_route(
    service: route_mod.RouteHistoryService,
    key: str,
) -> tuple[route_mod.FlightRouteInfo | None, bool]:
    cached = service._route_cache.get(key)
    if not cached:
        return None, False
    route = cached[1]
    ttl = (
        max(60, int(settings.route_lookup_cache_seconds))
        if route is not None
        else _NEGATIVE_CACHE_S
    )
    fresh = time.monotonic() - cached[0] < ttl
    return (route if fresh else None), fresh


async def _route_refresh(self: route_mod.RouteHistoryService, ac: Any) -> None:
    semaphore = getattr(self, "_route_guard_v2_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_ROUTE_REFRESH_CONCURRENCY)
        self._route_guard_v2_semaphore = semaphore
    async with semaphore:
        await legacy.resolve_route_resilient(self, ac)


def _schedule_route_refresh(self: route_mod.RouteHistoryService, ac: Any) -> None:
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key:
        return
    _, fresh = _cached_route(self, key)
    if fresh:
        return
    tasks: dict[str, asyncio.Task] = getattr(self, "_route_guard_v2_tasks", {})
    existing = tasks.get(key)
    if existing is not None and not existing.done():
        return

    if len(tasks) >= 32:
        logger.warning("route_refresh_saturated pending=%d", len(tasks))
        return

    task = asyncio.create_task(_route_refresh(self, ac), name=f"route-refresh:{key}")
    tasks[key] = task
    self._route_guard_v2_tasks = tasks

    def _finished(done: asyncio.Task, *, callsign: str = key) -> None:
        tasks.pop(callsign, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("route_guard_refresh_failed callsign=%s", callsign)

    task.add_done_callback(_finished)


async def observe_nonblocking(
    self: route_mod.RouteHistoryService,
    ac: Any,
    *,
    now: float | None = None,
) -> None:
    """Persist route samples off-path so Mongo latency cannot delay notifications."""
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key:
        return
    tasks: dict[str, asyncio.Task] = getattr(self, "_route_guard_v2_observe_tasks", {})
    existing = tasks.get(key)
    if existing is not None and not existing.done():
        return
    if len(tasks) >= _OBSERVE_TASK_LIMIT:
        logger.warning("route_history_background_saturated active=%d", len(tasks))
        return

    task = asyncio.create_task(
        _ORIGINAL_OBSERVE(self, ac, now=now),
        name=f"route-observe:{key}",
    )
    tasks[key] = task
    self._route_guard_v2_observe_tasks = tasks

    def _finished(done: asyncio.Task, *, callsign: str = key) -> None:
        tasks.pop(callsign, None)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("flight_route_observe_background_failed callsign=%s", callsign)

    task.add_done_callback(_finished)


async def _historical_paths_cached(
    self: route_mod.RouteHistoryService,
    key: str,
) -> list[list[route_mod.RoutePoint]]:
    cache: dict[str, tuple[float, list[list[route_mod.RoutePoint]]]] = getattr(
        self, "_route_guard_v2_history_cache", {}
    )
    now = time.monotonic()
    cached = cache.get(key)
    if cached and now - cached[0] < _HISTORY_CACHE_S:
        return cached[1]
    try:
        paths = await self._historical_paths(key)
    except Exception:
        logger.exception("flight_route_history_read_failed callsign=%s", key)
        paths = []
    cache[key] = (now, paths)
    if len(cache) > 256:
        oldest = sorted(cache.items(), key=lambda item: item[1][0])[:64]
        for old_key, _ in oldest:
            cache.pop(old_key, None)
    self._route_guard_v2_history_cache = cache
    return paths


def evaluate_route_gate_destination_aware(
    *,
    callsign: str,
    current_path: Iterable[Any],
    historical_paths: Iterable[Iterable[Any]],
    observer_lat: float,
    observer_lon: float,
    alert_radius_km: float,
    destination: route_mod.AirportInfo | None = None,
    route_plausible: bool = False,
    aircraft_lat: float | None = None,
    aircraft_lon: float | None = None,
    altitude_m: float | None = None,
    vertical_rate_mps: float | None = None,
    speed_kts: float | None = None,
    time_to_cpa_s: float | None = None,
    projected_path: Iterable[Any] | None = None,
    heading_deg: float | None = None,
) -> route_mod.RouteGateResult:
    """Extend the existing terminal-turn veto into a conservative preterminal window."""
    base = legacy.evaluate_route_gate_destination_aware(
        callsign=callsign,
        current_path=current_path,
        historical_paths=historical_paths,
        observer_lat=observer_lat,
        observer_lon=observer_lon,
        alert_radius_km=alert_radius_km,
        destination=destination,
        route_plausible=route_plausible,
        aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon,
        altitude_m=altitude_m,
        vertical_rate_mps=vertical_rate_mps,
        speed_kts=speed_kts,
        time_to_cpa_s=time_to_cpa_s,
        projected_path=projected_path,
        heading_deg=heading_deg,
    )
    if base.suppress_alert:
        return base
    if (
        not route_plausible
        or destination is None
        or destination.latitude is None
        or destination.longitude is None
        or aircraft_lat is None
        or aircraft_lon is None
        or time_to_cpa_s is None
        or float(time_to_cpa_s) < _PRETERMINAL_MIN_CPA_S
    ):
        return base

    current_observer = haversine_km(
        float(aircraft_lat), float(aircraft_lon), observer_lat, observer_lon
    )
    if current_observer <= alert_radius_km:
        return base

    destination_distance = haversine_km(
        float(aircraft_lat),
        float(aircraft_lon),
        float(destination.latitude),
        float(destination.longitude),
    )
    if destination_distance > _PRETERMINAL_DESTINATION_KM:
        return base

    observer_destination = haversine_km(
        observer_lat,
        observer_lon,
        float(destination.latitude),
        float(destination.longitude),
    )
    if observer_destination <= alert_radius_km + max(4.0, alert_radius_km * 0.25):
        return base

    cpa_destination = legacy._projected_cpa_destination_distance(
        projected_path,
        observer_lat=observer_lat,
        observer_lon=observer_lon,
        destination=destination,
    )
    if cpa_destination is None:
        return base

    outward_growth = cpa_destination - destination_distance
    required_growth = max(6.0, min(14.0, destination_distance * 0.08))
    if outward_growth < required_growth:
        return base

    if heading_deg is not None:
        destination_bearing = bearing_deg(
            float(aircraft_lat),
            float(aircraft_lon),
            float(destination.latitude),
            float(destination.longitude),
        )
        if abs(_angle_delta(float(heading_deg), destination_bearing)) < 24.0:
            return base

    return replace(
        base,
        suppress_alert=True,
        reason=(
            f"known destination {destination.code} requires a turn before observer CPA: "
            "straight-line projection materially diverges from the destination"
        ),
        destination_code=destination.code,
        destination_distance_km=destination_distance,
        expected_turn_pending=True,
        route_plausible=True,
    )


def _directly_inside(pred: Any, alert_radius_km: float) -> bool:
    try:
        return (
            not bool(getattr(pred, "stale", False))
            and float(getattr(pred, "current_distance_km")) <= float(alert_radius_km)
        )
    except (TypeError, ValueError):
        return False


def _bounded_pending_gate(
    self: route_mod.RouteHistoryService,
    key: str,
    result: route_mod.RouteGateResult,
    *,
    directly_inside: bool,
) -> route_mod.RouteGateResult:
    pending: dict[str, float] = getattr(self, "_route_guard_v2_pending_since", {})
    now = time.monotonic()
    for stale_key, started in list(pending.items()):
        if now - started > 600.0:
            pending.pop(stale_key, None)

    if directly_inside or result.suppress_alert:
        pending.pop(key, None)
        self._route_guard_v2_pending_since = pending
        return result

    started = pending.setdefault(key, now)
    self._route_guard_v2_pending_since = pending
    elapsed = now - started
    if elapsed < _INITIAL_ROUTE_GRACE_S:
        return replace(
            result,
            suppress_alert=True,
            reason=(
                "route resolution pending in background before first projected approach alert "
                f"({elapsed:.1f}s/{_INITIAL_ROUTE_GRACE_S:.0f}s max)"
            ),
        )
    return result


async def evaluate_route_nonblocking(
    self: route_mod.RouteHistoryService,
    ac: Any,
    pred: Any,
    *,
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    current_samples: Iterable[Any],
) -> route_mod.RouteGateResult:
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key:
        return route_mod.RouteGateResult(False, "", "no usable flight-number callsign")

    history = await _historical_paths_cached(self, key)
    route, cache_fresh = _cached_route(self, key)
    if not cache_fresh:
        _schedule_route_refresh(self, ac)

    result = evaluate_route_gate_destination_aware(
        callsign=key,
        current_path=current_samples,
        historical_paths=history,
        observer_lat=user_lat,
        observer_lon=user_lon,
        alert_radius_km=alert_radius_km,
        destination=route.destination if route else None,
        route_plausible=bool(route and route.plausible),
        aircraft_lat=getattr(ac, "latitude", None),
        aircraft_lon=getattr(ac, "longitude", None),
        altitude_m=getattr(ac, "altitude", None),
        vertical_rate_mps=getattr(ac, "vertical_rate_mps", None),
        speed_kts=getattr(ac, "ground_speed", None),
        time_to_cpa_s=getattr(pred, "time_to_cpa_s", None),
        projected_path=getattr(pred, "path", None),
        heading_deg=getattr(ac, "heading", None),
    )

    directly_inside = _directly_inside(pred, alert_radius_km)
    if route is None:
        result = _bounded_pending_gate(
            self,
            key,
            result,
            directly_inside=directly_inside,
        )
    else:
        pending: dict[str, float] = getattr(self, "_route_guard_v2_pending_since", {})
        pending.pop(key, None)
        self._route_guard_v2_pending_since = pending

    if directly_inside and result.suppress_alert:
        result = replace(
            result,
            suppress_alert=False,
            reason="direct observed presence inside alert radius overrides route veto",
            expected_turn_pending=False,
        )

    source = getattr(self, "_route_guard_sources", {}).get(key, "")
    logger.info(
        "route_gate callsign=%s suppress=%s history_days=%d similar_days=%d similarity_km=%s destination=%s expected_turn=%s source=%s cache_fresh=%s reason=%s",
        result.callsign,
        result.suppress_alert,
        result.history_days,
        result.similar_days,
        f"{result.similarity_km:.2f}" if result.similarity_km is not None else "na",
        result.destination_code or "unknown",
        result.expected_turn_pending,
        source or "none",
        cache_fresh,
        result.reason,
    )
    return result


def install_route_guard_v2() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    legacy._choose_route = _choose_route_prefer_position_aware
    route_mod.RouteHistoryService.observe = observe_nonblocking
    route_mod.RouteHistoryService.evaluate = evaluate_route_nonblocking
    _INSTALLED = True
    logger.info(
        "Route guard v2 enabled: non-blocking history writes, background route lookup, %.0fs max initial route grace, preterminal turn veto",
        _INITIAL_ROUTE_GRACE_S,
    )
