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
from typing import Any, Iterable
from urllib.parse import quote

from app.aircraft.providers import get_http_client
from app.config import settings
from app.intelligence import route_history as route_mod
from app.intelligence.trajectory import bearing_deg, haversine_km

logger = logging.getLogger(__name__)

_ADSBDB_CALLSIGN = "https://api.adsbdb.com/v0/callsign"
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


def _same_airport(
    a: route_mod.AirportInfo | None,
    b: route_mod.AirportInfo | None,
) -> bool:
    if a is None or b is None:
        return False
    codes_a = {value.upper() for value in (a.icao, a.iata) if value}
    codes_b = {value.upper() for value in (b.icao, b.iata) if value}
    if codes_a and codes_b:
        return bool(codes_a & codes_b)
    return bool(
        a.name
        and b.name
        and a.name.strip().casefold() == b.name.strip().casefold()
    )


def _route_is_position_plausible(
    origin: route_mod.AirportInfo | None,
    destination: route_mod.AirportInfo | None,
    latitude: float,
    longitude: float,
) -> bool:
    """Reject static callsign rows that are physically unrelated to the live aircraft."""
    if (
        origin is None
        or destination is None
        or origin.latitude is None
        or origin.longitude is None
        or destination.latitude is None
        or destination.longitude is None
    ):
        return False

    to_origin = haversine_km(
        latitude, longitude, origin.latitude, origin.longitude
    )
    to_destination = haversine_km(
        latitude, longitude, destination.latitude, destination.longitude
    )
    if min(to_origin, to_destination) <= 320.0:
        return True

    direct = haversine_km(
        origin.latitude,
        origin.longitude,
        destination.latitude,
        destination.longitude,
    )
    if direct < 25.0:
        return False
    route_via_aircraft = to_origin + to_destination
    excess = max(0.0, route_via_aircraft - direct)
    return excess <= max(180.0, direct * 0.18)


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
        and _route_is_position_plausible(
            route.origin,
            route.destination,
            latitude,
            longitude,
        )
    )


def _airport_from_adsbdb(raw: Any) -> route_mod.AirportInfo | None:
    if not isinstance(raw, dict):
        return None
    try:
        latitude = (
            float(raw["latitude"]) if raw.get("latitude") is not None else None
        )
        longitude = (
            float(raw["longitude"]) if raw.get("longitude") is not None else None
        )
    except (TypeError, ValueError):
        latitude = longitude = None
    return route_mod.AirportInfo(
        icao=str(raw.get("icao_code") or "").upper(),
        iata=str(raw.get("iata_code") or "").upper(),
        name=str(raw.get("name") or ""),
        latitude=latitude,
        longitude=longitude,
    )


def _route_from_adsbdb(
    callsign: str,
    payload: Any,
    *,
    latitude: float,
    longitude: float,
) -> route_mod.FlightRouteInfo | None:
    if not isinstance(payload, dict):
        return None
    response = payload.get("response")
    if not isinstance(response, dict):
        return None
    raw = response.get("flightroute")
    if not isinstance(raw, dict):
        return None

    origin = _airport_from_adsbdb(raw.get("origin"))
    destination = _airport_from_adsbdb(raw.get("destination"))
    if origin is None or destination is None:
        return None
    plausible = _route_is_position_plausible(
        origin, destination, float(latitude), float(longitude)
    )
    return route_mod.FlightRouteInfo(
        callsign=callsign,
        airport_codes=f"{origin.code}-{destination.code}",
        plausible=plausible,
        origin=origin,
        destination=destination,
    )


def _terminal_candidate(
    route: route_mod.FlightRouteInfo,
    *,
    latitude: float,
    longitude: float,
    altitude_m: float | None,
    vertical_rate_mps: float | None,
    velocity_mps: float | None,
) -> tuple[bool, float]:
    destination = route.destination
    if (
        destination is None
        or destination.latitude is None
        or destination.longitude is None
    ):
        return False, math.inf

    destination_distance = haversine_km(
        latitude,
        longitude,
        destination.latitude,
        destination.longitude,
    )
    if destination_distance > _TERMINAL_CONTEXT_KM:
        return False, destination_distance

    try:
        altitude = float(altitude_m) if altitude_m is not None else math.inf
    except (TypeError, ValueError):
        altitude = math.inf
    try:
        vertical_rate = (
            float(vertical_rate_mps) if vertical_rate_mps is not None else 0.0
        )
    except (TypeError, ValueError):
        vertical_rate = 0.0
    try:
        speed = float(velocity_mps) / 1000.0 if velocity_mps is not None else 0.0
    except (TypeError, ValueError):
        speed = 0.0

    descending = vertical_rate <= -0.35
    low = altitude <= _TERMINAL_ALTITUDE_M
    eta_destination = (
        destination_distance / speed if speed > 0.04 else math.inf
    )

    origin_distance = math.inf
    origin = route.origin
    if (
        origin is not None
        and origin.latitude is not None
        and origin.longitude is not None
    ):
        origin_distance = haversine_km(
            latitude, longitude, origin.latitude, origin.longitude
        )

    arrival_side = (
        destination_distance + 20.0 < origin_distance
        or descending
        or destination_distance <= 55.0
    )
    terminal = (
        arrival_side
        and (
            descending
            or low
            or eta_destination <= 22.0 * 60.0
        )
    )
    return terminal, destination_distance


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
        self,
        callsign: str,
        latitude: float,
        longitude: float,
    ) -> route_mod.FlightRouteInfo | None:
        client = await get_http_client()

        single_url = str(
            getattr(settings, "route_lookup_single_url", "") or ""
        ).strip()
        if single_url:
            response = None
            try:
                url = (
                    f"{single_url.rstrip('/')}/{quote(callsign, safe='')}"
                    f"/{latitude:.5f}/{longitude:.5f}"
                )
                response = await client.get(url, timeout=_LOOKUP_TIMEOUT_S)
                response.raise_for_status()
                route = route_mod._route_info(callsign, response.json())
                if _usable(route, latitude, longitude):
                    return route
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info(
                    "destination_source_attempt_failed callsign=%s "
                    "source=adsb.lol-single status=%s error=%s",
                    callsign,
                    getattr(response, "status_code", "na"),
                    type(exc).__name__,
                )

        bulk_url = str(getattr(settings, "route_lookup_url", "") or "").strip()
        if not bulk_url:
            return None
        response = None
        try:
            response = await client.post(
                bulk_url,
                json={
                    "planes": [
                        {
                            "callsign": callsign,
                            "lat": latitude,
                            "lng": longitude,
                        }
                    ]
                },
                timeout=_LOOKUP_TIMEOUT_S,
            )
            response.raise_for_status()
            route = route_mod._route_info(callsign, response.json())
            return route if _usable(route, latitude, longitude) else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "destination_source_failed callsign=%s source=adsb.lol "
                "status=%s error=%s",
                callsign,
                getattr(response, "status_code", "na"),
                type(exc).__name__,
            )
            return None

    async def _fetch_adsbdb(
        self,
        callsign: str,
        latitude: float,
        longitude: float,
    ) -> route_mod.FlightRouteInfo | None:
        client = await get_http_client()
        response = None
        try:
            response = await client.get(
                f"{_ADSBDB_CALLSIGN}/{quote(callsign, safe='')}",
                timeout=_LOOKUP_TIMEOUT_S,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            route = _route_from_adsbdb(
                callsign,
                response.json(),
                latitude=latitude,
                longitude=longitude,
            )
            return route if _usable(route, latitude, longitude) else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info(
                "destination_source_failed callsign=%s source=adsbdb "
                "status=%s error=%s",
                callsign,
                getattr(response, "status_code", "na"),
                type(exc).__name__,
            )
            return None

    @staticmethod
    def choose(
        callsign: str,
        adsb_lol: route_mod.FlightRouteInfo | None,
        adsbdb: route_mod.FlightRouteInfo | None,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        altitude_m: float | None = None,
        vertical_rate_mps: float | None = None,
        velocity_mps: float | None = None,
    ) -> tuple[DestinationResolution | None, str]:
        if adsb_lol and adsbdb:
            if _same_airport(adsb_lol.destination, adsbdb.destination):
                return (
                    DestinationResolution(
                        adsb_lol,
                        "adsb.lol+adsbdb",
                        "provider-agreement",
                    ),
                    "resolved",
                )

            logger.warning(
                "destination_source_conflict callsign=%s adsb_lol=%s adsbdb=%s",
                callsign,
                adsb_lol.destination.code if adsb_lol.destination else "unknown",
                adsbdb.destination.code if adsbdb.destination else "unknown",
            )

            # Provider disagreement is normally ambiguous and fails open. One
            # exception is intentionally deterministic: if exactly one provider
            # points at a physically strong nearby terminal destination while
            # the competing destination is far away, live position/descent can
            # resolve the conflict without trusting provider brand priority.
            if latitude is not None and longitude is not None:
                rows: list[
                    tuple[
                        str,
                        route_mod.FlightRouteInfo,
                        bool,
                        float,
                    ]
                ] = []
                for source, route in (
                    ("adsb.lol", adsb_lol),
                    ("adsbdb", adsbdb),
                ):
                    terminal, distance = _terminal_candidate(
                        route,
                        latitude=float(latitude),
                        longitude=float(longitude),
                        altitude_m=altitude_m,
                        vertical_rate_mps=vertical_rate_mps,
                        velocity_mps=velocity_mps,
                    )
                    rows.append((source, route, terminal, distance))

                strong = [row for row in rows if row[2]]
                if len(strong) == 1:
                    selected = strong[0]
                    other = rows[0] if rows[1] is selected else rows[1]
                    if other[3] >= max(
                        _TERMINAL_CONTEXT_KM,
                        selected[3] + 120.0,
                        selected[3] * 2.5,
                    ):
                        logger.warning(
                            "destination_conflict_resolved_by_live_geometry "
                            "callsign=%s source=%s destination=%s "
                            "destination_km=%.1f competing_km=%.1f",
                            callsign,
                            selected[0],
                            selected[1].destination.code,
                            selected[3],
                            other[3],
                        )
                        return (
                            DestinationResolution(
                                selected[1],
                                selected[0],
                                "live-terminal-conflict-resolution",
                            ),
                            "resolved",
                        )
            return None, "conflict"

        route = adsb_lol or adsbdb
        if route is None:
            return None, "unavailable"
        source = "adsb.lol" if adsb_lol else "adsbdb"
        return (
            DestinationResolution(route, source, "single-provider"),
            "resolved",
        )

    async def _refresh(
        self,
        key: str,
        latitude: float,
        longitude: float,
        *,
        altitude_m: float | None = None,
        vertical_rate_mps: float | None = None,
        velocity_mps: float | None = None,
    ) -> None:
        started = time.monotonic()
        try:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
            async with self._semaphore:
                adsb_lol, adsbdb = await asyncio.gather(
                    self._fetch_adsb_lol(key, latitude, longitude),
                    self._fetch_adsbdb(key, latitude, longitude),
                )
            resolution, state = self.choose(
                key,
                adsb_lol,
                adsbdb,
                latitude=latitude,
                longitude=longitude,
                altitude_m=altitude_m,
                vertical_rate_mps=vertical_rate_mps,
                velocity_mps=velocity_mps,
            )
            if resolution is not None:
                self._put(
                    key,
                    resolution,
                    "resolved",
                    float(max(60, int(settings.route_lookup_cache_seconds))),
                )
                logger.info(
                    "destination_resolved callsign=%s source=%s authority=%s "
                    "destination=%s latency_ms=%.1f",
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
        latitude = getattr(ac, "latitude", None)
        longitude = getattr(ac, "longitude", None)
        if not key or latitude is None or longitude is None:
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
                self._refresh(
                    key,
                    float(latitude),
                    float(longitude),
                    altitude_m=getattr(ac, "altitude", None),
                    vertical_rate_mps=getattr(ac, "vertical_rate_mps", None),
                    velocity_mps=getattr(ac, "velocity", None),
                ),
                name=f"destination-refresh:{key}",
            )
        except RuntimeError:
            return None, False
        self._inflight[key] = task

        def done(
            finished: asyncio.Task,
            callsign: str = key,
        ) -> None:
            if self._inflight.get(callsign) is finished:
                self._inflight.pop(callsign, None)
            if finished.cancelled():
                return
            try:
                finished.exception()
            except Exception:
                logger.exception(
                    "destination_background_task_failed callsign=%s",
                    callsign,
                )

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


def _diverged(
    ac: Any,
    samples: Iterable[Any],
    destination: route_mod.AirportInfo,
) -> bool:
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
    old_distance = haversine_km(
        oldest[1],
        oldest[2],
        destination.latitude,
        destination.longitude,
    )
    new_distance = haversine_km(
        newest[1],
        newest[2],
        destination.latitude,
        destination.longitude,
    )
    increase = new_distance - old_distance

    try:
        track = float(getattr(ac, "heading"))
        destination_bearing = bearing_deg(
            newest[1],
            newest[2],
            destination.latitude,
            destination.longitude,
        )
        heading_away = (
            abs(_angle_delta(track, destination_bearing)) >= 70.0
        )
    except (TypeError, ValueError):
        heading_away = False

    try:
        climbing = float(getattr(ac, "vertical_rate_mps")) >= 2.0
    except (TypeError, ValueError):
        climbing = False

    # Climb + increasing airport distance is a general missed-approach/diversion
    # signal and may release quickly. Ordinary terminal vectoring cannot.
    if climbing and increase >= max(
        0.8,
        min(2.0, new_distance * 0.015),
    ):
        return True

    if span < 30.0:
        return False
    return (
        heading_away
        and increase >= max(
            3.0,
            min(8.0, new_distance * 0.05),
        )
    )


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
        cpa_destination = min(
            rows,
            key=lambda row: abs(row[0] - cpa_time),
        )[1]
    except (TypeError, ValueError):
        cpa_destination = None

    horizon = (
        candidate_time
        if candidate_time is not None
        else math.inf
    )
    landing = [
        seconds
        for seconds, distance in rows
        if seconds <= horizon and distance <= _LANDING_AREA_KM
    ]
    return cpa_destination, min(landing) if landing else None


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
        return _result(
            "",
            False,
            "no route callsign; live trajectory authoritative",
        )

    latitude = getattr(ac, "latitude", None)
    longitude = getattr(ac, "longitude", None)

    current_distance = math.inf
    if latitude is not None and longitude is not None:
        current_distance = haversine_km(
            float(latitude),
            float(longitude),
            float(user_lat),
            float(user_lon),
        )
    else:
        try:
            current_distance = float(pred.current_distance_km)
        except (AttributeError, TypeError, ValueError):
            pass

    # Accepted live ADS-B position is physical truth. Destination metadata can
    # never hide a fresh aircraft that has already entered the user's radius.
    if current_distance <= float(alert_radius_km):
        return _result(
            key,
            False,
            "fresh physical presence inside alert radius overrides destination metadata",
        )

    entry, pending = destination_resolver.lookup(ac)
    if entry is None:
        # A brand-new candidate can wait one monitor cycle for bounded
        # background resolution. Existing alerts are never cancelled only
        # because provider work is pending.
        hold = bool(pending and not notification_sent)
        return _result(
            key,
            hold,
            (
                "destination lookup pending in background; "
                "initial alert held without blocking"
            )
            if hold
            else "destination unavailable; live trajectory authoritative",
        )

    if entry.resolution is None:
        return _result(
            key,
            False,
            (
                "destination providers disagree; live trajectory authoritative"
                if entry.state == "conflict"
                else "destination unavailable; live trajectory authoritative"
            ),
        )

    resolution = entry.resolution
    destination = resolution.route.destination
    if (
        destination is None
        or latitude is None
        or longitude is None
    ):
        return _result(
            key,
            False,
            "destination geometry incomplete; live trajectory authoritative",
        )

    destination_distance = haversine_km(
        float(latitude),
        float(longitude),
        destination.latitude,
        destination.longitude,
    )

    if _diverged(ac, current_samples, destination):
        return _result(
            key,
            False,
            (
                "live movement diverges from reported destination; "
                "live trajectory regained authority"
            ),
            destination,
            destination_distance,
        )

    observer_destination = haversine_km(
        float(user_lat),
        float(user_lon),
        destination.latitude,
        destination.longitude,
    )
    # If the airport itself is within the configured radius, an arrival can
    # genuinely enter that radius while landing. Fail open rather than using
    # destination metadata to hide that real nearby presence.
    if observer_destination <= float(alert_radius_km):
        return _result(
            key,
            False,
            (
                "destination airport lies inside observer radius; "
                "live trajectory authoritative"
            ),
            destination,
            destination_distance,
        )

    speed = _speed_km_s(ac)
    eta_destination = (
        destination_distance / speed if speed > 0.04 else None
    )
    candidate_time = _candidate_time(pred)
    cpa_destination, landing_time = _path_geometry(
        pred,
        destination,
        candidate_time,
    )

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
            or (
                eta_destination is not None
                and eta_destination <= 22.0 * 60.0
            )
        )
    )
    if not terminal:
        return _result(
            key,
            False,
            (
                "destination is not yet strong terminal-arrival evidence; "
                "live trajectory authoritative"
            ),
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
            (
                f"{resolution.source}: projected path reaches destination "
                "landing area before observer pass"
            ),
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
            (
                f"{resolution.source}: destination airport is physically "
                "reached before projected observer pass"
            ),
            destination,
            destination_distance,
        )

    if cpa_destination is not None:
        away_margin = max(
            5.0,
            min(18.0, destination_distance * 0.10),
        )
        if cpa_destination >= destination_distance + away_margin:
            return _result(
                key,
                True,
                (
                    f"{resolution.source}: observer CPA would move aircraft "
                    "significantly away from known destination"
                ),
                destination,
                destination_distance,
            )

    return _result(
        key,
        False,
        (
            f"{resolution.source}: destination path remains compatible with "
            "a genuine observer pass before landing"
        ),
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
