"""Single provider-first destination/path authority for live approach alerts.

Route history and runway inference remain useful diagnostics, but they do not decide
live alert qualification here. Destination lookups are bounded background work:
the five-second monitor never awaits an external route provider.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable

from app.aircraft.providers import get_http_client
from app.config import settings
from app.intelligence import route_guard as provider_helpers
from app.intelligence import route_history as route_mod
from app.intelligence.trajectory import bearing_deg, haversine_km

logger = logging.getLogger(__name__)

_LOOKUP_TIMEOUT_S = 2.2
_NEGATIVE_TTL_S = 30.0
_CONFLICT_TTL_S = 60.0
_MAX_CACHE = 4096
_MAX_INFLIGHT = 24
_MAX_CONCURRENT = 4
_TERMINAL_CONTEXT_KM = 180.0
_TERMINAL_ALTITUDE_M = 8500.0
_LANDING_AREA_KM = 5.0
_AIRPORT_FIRST_MARGIN_S = 35.0
_INSTALLED = False


@dataclass(slots=True, frozen=True)
class DestinationResolution:
    route: route_mod.FlightRouteInfo
    source: str
    authority: str


@dataclass(slots=True)
class CacheEntry:
    resolution: DestinationResolution | None
    state: str
    expires_at: float


def _same_airport(a: route_mod.AirportInfo | None, b: route_mod.AirportInfo | None) -> bool:
    if a is None or b is None:
        return False
    codes_a = {value.upper() for value in (a.icao, a.iata) if value}
    codes_b = {value.upper() for value in (b.icao, b.iata) if value}
    if codes_a and codes_b:
        return bool(codes_a & codes_b)
    return bool(a.name and b.name and a.name.strip().casefold() == b.name.strip().casefold())


def _usable(
    route: route_mod.FlightRouteInfo | None,
    latitude: float,
    longitude: float,
) -> bool:
    return bool(
        route
        and route.plausible
        and route.destination
        and route.destination.latitude is not None
        and route.destination.longitude is not None
        and provider_helpers._route_is_position_plausible(
            route.origin, route.destination, latitude, longitude
        )
    )


class DestinationResolver:
    """Bounded ADSB.lol + ADSBDB resolver with no monitor-path network awaits."""

    def __init__(self) -> None:
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._inflight: dict[str, asyncio.Task] = {}
        self._semaphore: asyncio.Semaphore | None = None

    def clear(self) -> None:
        for task in self._inflight.values():
            if not task.done():
                task.cancel()
        self._cache.clear()
        self._inflight.clear()
        self._semaphore = None

    def _put(
        self,
        key: str,
        resolution: DestinationResolution | None,
        state: str,
        ttl_s: float,
    ) -> None:
        self._cache[key] = CacheEntry(
            resolution=resolution,
            state=state,
            expires_at=time.monotonic() + max(1.0, ttl_s),
        )
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHE:
            self._cache.popitem(last=False)

    def cached(self, key: str) -> CacheEntry | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return entry

    async def _fetch_adsb_lol(
        self, callsign: str, latitude: float, longitude: float
    ) -> route_mod.FlightRouteInfo | None:
        aircraft = SimpleNamespace(
            callsign=callsign, latitude=latitude, longitude=longitude
        )
        try:
            route = await asyncio.wait_for(
                route_mod.route_history_service.resolve_route(aircraft),
                timeout=_LOOKUP_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "destination_source_failed callsign=%s source=adsb.lol error=%s",
                callsign,
                type(exc).__name__,
            )
            return None
        return route if _usable(route, latitude, longitude) else None

    async def _fetch_adsbdb(
        self, callsign: str, latitude: float, longitude: float
    ) -> route_mod.FlightRouteInfo | None:
        try:
            client = await get_http_client()
            route = await asyncio.wait_for(
                provider_helpers._fetch_adsbdb(
                    client, callsign, latitude, longitude
                ),
                timeout=_LOOKUP_TIMEOUT_S + 0.2,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "destination_source_failed callsign=%s source=adsbdb error=%s",
                callsign,
                type(exc).__name__,
            )
            return None
        return route if _usable(route, latitude, longitude) else None

    @staticmethod
    def choose(
        callsign: str,
        adsb_lol: route_mod.FlightRouteInfo | None,
        adsbdb: route_mod.FlightRouteInfo | None,
    ) -> tuple[DestinationResolution | None, str]:
        if adsb_lol and adsbdb:
            if not _same_airport(adsb_lol.destination, adsbdb.destination):
                logger.warning(
                    "destination_source_conflict callsign=%s adsb_lol=%s adsbdb=%s",
                    callsign,
                    adsb_lol.destination.code,
                    adsbdb.destination.code,
                )
                return None, "conflict"
            return (
                DestinationResolution(
                    adsb_lol, "adsb.lol+adsbdb", "provider-agreement"
                ),
                "resolved",
            )
        route = adsb_lol or adsbdb
        if route is None:
            return None, "unavailable"
        source = "adsb.lol" if adsb_lol else "adsbdb"
        return DestinationResolution(route, source, "single-provider"), "resolved"

    async def _refresh(self, key: str, latitude: float, longitude: float) -> None:
        started = time.monotonic()
        try:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
            async with self._semaphore:
                lol, db = await asyncio.gather(
                    self._fetch_adsb_lol(key, latitude, longitude),
                    self._fetch_adsbdb(key, latitude, longitude),
                )
            resolution, state = self.choose(key, lol, db)
            if resolution is not None:
                self._put(
                    key,
                    resolution,
                    "resolved",
                    float(max(60, int(settings.route_lookup_cache_seconds))),
                )
                logger.info(
                    "destination_resolved callsign=%s source=%s authority=%s destination=%s latency_ms=%.1f",
                    key,
                    resolution.source,
                    resolution.authority,
                    resolution.route.destination.code,
                    (time.monotonic() - started) * 1000.0,
                )
            elif state == "conflict":
                self._put(key, None, state, _CONFLICT_TTL_S)
            else:
                self._put(key, None, "unavailable", _NEGATIVE_TTL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("destination_refresh_failed callsign=%s", key)
            self._put(key, None, "unavailable", _NEGATIVE_TTL_S)

    def lookup(self, ac: Any) -> tuple[CacheEntry | None, bool]:
        key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
        lat, lon = getattr(ac, "latitude", None), getattr(ac, "longitude", None)
        if not key or lat is None or lon is None:
            return None, False
        cached = self.cached(key)
        if cached is not None:
            return cached, False
        task = self._inflight.get(key)
        if task is not None and not task.done():
            return None, True
        if len(self._inflight) >= _MAX_INFLIGHT:
            return None, False
        try:
            task = asyncio.create_task(
                self._refresh(key, float(lat), float(lon)),
                name=f"destination-refresh:{key}",
            )
        except RuntimeError:
            return None, False
        self._inflight[key] = task

        def done(finished: asyncio.Task, callsign: str = key) -> None:
            if self._inflight.get(callsign) is finished:
                self._inflight.pop(callsign, None)
            if finished.cancelled():
                return
            try:
                finished.exception()
            except Exception:
                logger.exception("destination_background_task_failed callsign=%s", callsign)

        task.add_done_callback(done)
        return None, True


destination_resolver = DestinationResolver()


def _result(
    key: str,
    suppress: bool,
    reason: str,
    destination: route_mod.AirportInfo | None = None,
    destination_distance: float | None = None,
) -> route_mod.RouteGateResult:
    return route_mod.RouteGateResult(
        suppress_alert=suppress,
        callsign=key,
        reason=reason,
        destination_code=destination.code if destination else "",
        destination_distance_km=destination_distance,
        expected_turn_pending=suppress,
        route_plausible=destination is not None,
    )


def _angle_delta(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _diverged(ac: Any, samples: Iterable[Any], destination: route_mod.AirportInfo) -> bool:
    """Require sustained divergence, except a clear climbing go-around."""
    rows = []
    for sample in samples:
        try:
            rows.append(
                (
                    float(sample.timestamp),
                    float(sample.latitude),
                    float(sample.longitude),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    rows.sort()
    if len(rows) < 2:
        return False
    newest = rows[-1]
    oldest = next(
        (row for row in rows if newest[0] - row[0] >= 8.0),
        None,
    )
    if oldest is None:
        return False
    span = newest[0] - oldest[0]
    old_d = haversine_km(
        oldest[1], oldest[2], destination.latitude, destination.longitude
    )
    new_d = haversine_km(
        newest[1], newest[2], destination.latitude, destination.longitude
    )
    increase = new_d - old_d
    try:
        track = float(getattr(ac, "heading"))
        dest_bearing = bearing_deg(
            newest[1], newest[2], destination.latitude, destination.longitude
        )
        heading_away = abs(_angle_delta(track, dest_bearing)) >= 70.0
    except (TypeError, ValueError):
        heading_away = False
    try:
        climbing = float(getattr(ac, "vertical_rate_mps")) >= 2.0
    except (TypeError, ValueError):
        climbing = False

    # A climbing aircraft that is already increasing its airport distance is a
    # general go-around/diversion signal and can release quickly.
    if climbing and increase >= max(0.8, min(2.0, new_d * 0.015)):
        return True

    # Heading away for 5-10 seconds is normal on terminal arrivals and was the
    # original false-alert failure. Require a much longer, material divergence
    # before treating provider destination data as stale/wrong.
    if span < 30.0:
        return False
    return heading_away and increase >= max(3.0, min(8.0, new_d * 0.05))


def _speed_km_s(ac: Any) -> float:
    try:
        velocity = float(getattr(ac, "velocity"))
        if velocity > 1.0:
            return velocity / 1000.0
    except (TypeError, ValueError):
        pass
    try:
        knots = float(getattr(ac, "ground_speed"))
        if knots > 1.0:
            return knots * 0.0005144444444444444
    except (TypeError, ValueError):
        pass
    return 0.0


def _candidate_time(pred: Any) -> float | None:
    for name in ("radius_entry_s", "time_to_cpa_s"):
        try:
            value = float(getattr(pred, name))
            if math.isfinite(value) and value >= 0.0:
                return value
        except (TypeError, ValueError):
            pass
    return None


def _path_geometry(
    pred: Any,
    destination: route_mod.AirportInfo,
    candidate_time: float | None,
) -> tuple[float | None, float | None]:
    """Return destination distance at observer CPA and first airport-area time."""
    rows = []
    for point in list(getattr(pred, "path", None) or []):
        try:
            rows.append(
                (
                    float(point.seconds),
                    haversine_km(
                        float(point.latitude),
                        float(point.longitude),
                        destination.latitude,
                        destination.longitude,
                    ),
                )
            )
        except (AttributeError, TypeError, ValueError):
            continue
    if not rows:
        return None, None
    try:
        cpa_time = float(getattr(pred, "time_to_cpa_s"))
        cpa_dest = min(rows, key=lambda row: abs(row[0] - cpa_time))[1]
    except (TypeError, ValueError):
        cpa_dest = None
    horizon = candidate_time if candidate_time is not None else math.inf
    landing = [seconds for seconds, distance in rows if seconds <= horizon and distance <= _LANDING_AREA_KM]
    return cpa_dest, min(landing) if landing else None


async def evaluate_destination_path(
    self: route_mod.RouteHistoryService,
    ac: Any,
    pred: Any,
    *,
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    current_samples: Iterable[Any],
    notification_sent: bool = False,
) -> route_mod.RouteGateResult:
    """One deterministic destination/path decision; performs no network await."""
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key:
        return _result("", False, "no route callsign; live trajectory authoritative")

    try:
        current_distance = float(pred.current_distance_km)
    except (AttributeError, TypeError, ValueError):
        current_distance = math.inf
    if current_distance <= float(alert_radius_km):
        return _result(
            key,
            False,
            "fresh physical presence inside alert radius overrides destination metadata",
        )

    entry, pending = destination_resolver.lookup(ac)
    if entry is None:
        # A new alert can wait one monitor cycle for background destination
        # resolution. Existing alerts are never cancelled due to a pending lookup.
        hold = bool(pending and not notification_sent)
        return _result(
            key,
            hold,
            "destination lookup pending in background; initial alert held without blocking"
            if hold
            else "destination unavailable; live trajectory authoritative",
        )
    if entry.resolution is None:
        return _result(
            key,
            False,
            "destination providers disagree; live trajectory authoritative"
            if entry.state == "conflict"
            else "destination unavailable; live trajectory authoritative",
        )

    resolution = entry.resolution
    destination = resolution.route.destination
    lat, lon = getattr(ac, "latitude", None), getattr(ac, "longitude", None)
    if destination is None or lat is None or lon is None:
        return _result(key, False, "destination geometry incomplete; live trajectory authoritative")

    destination_distance = haversine_km(
        float(lat), float(lon), destination.latitude, destination.longitude
    )
    if _diverged(ac, current_samples, destination):
        return _result(
            key,
            False,
            "live movement diverges from reported destination; live trajectory regained authority",
            destination,
            destination_distance,
        )

    observer_destination = haversine_km(
        float(user_lat), float(user_lon), destination.latitude, destination.longitude
    )
    if observer_destination <= float(alert_radius_km) + 1.0:
        return _result(
            key,
            False,
            "observer lies in destination-area pass geometry; live trajectory authoritative",
            destination,
            destination_distance,
        )

    speed = _speed_km_s(ac)
    eta_destination = destination_distance / speed if speed > 0.04 else None
    candidate_time = _candidate_time(pred)
    cpa_destination, landing_time = _path_geometry(pred, destination, candidate_time)

    try:
        altitude = float(getattr(ac, "altitude"))
    except (TypeError, ValueError):
        altitude = math.inf
    try:
        vertical_rate = float(getattr(ac, "vertical_rate_mps"))
    except (TypeError, ValueError):
        vertical_rate = 0.0

    descending = vertical_rate <= -0.35
    low = altitude <= _TERMINAL_ALTITUDE_M
    terminal = (
        destination_distance <= _TERMINAL_CONTEXT_KM
        and (
            descending
            or low
            or (eta_destination is not None and eta_destination <= 22.0 * 60.0)
        )
    )
    if not terminal:
        return _result(
            key,
            False,
            "destination is not yet strong terminal-arrival evidence; live trajectory authoritative",
            destination,
            destination_distance,
        )

    if (
        landing_time is not None
        and candidate_time is not None
        and landing_time + 5.0 < candidate_time
    ):
        return _result(
            key,
            True,
            f"{resolution.source}: projected path reaches destination landing area before observer pass",
            destination,
            destination_distance,
        )

    if (
        eta_destination is not None
        and candidate_time is not None
        and eta_destination + _AIRPORT_FIRST_MARGIN_S < candidate_time
        and (descending or low)
    ):
        return _result(
            key,
            True,
            f"{resolution.source}: destination airport is physically reached before projected observer pass",
            destination,
            destination_distance,
        )

    if cpa_destination is not None:
        away_margin = max(5.0, min(18.0, destination_distance * 0.10))
        if cpa_destination >= destination_distance + away_margin:
            return _result(
                key,
                True,
                f"{resolution.source}: observer CPA would move aircraft significantly away from known destination",
                destination,
                destination_distance,
            )

    return _result(
        key,
        False,
        f"{resolution.source}: destination path remains compatible with a genuine observer pass before landing",
        destination,
        destination_distance,
    )


def install_destination_path_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    route_mod.RouteHistoryService.evaluate = evaluate_destination_path
    _INSTALLED = True
    logger.info(
        "Destination path guard enabled providers=adsb.lol,adsbdb "
        "history_authority=false runway_authority=false nonblocking=true"
    )
