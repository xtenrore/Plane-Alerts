"""Flight-number route-history gate for Plane? spotting alerts.

This module deliberately keys historical routing by the transmitted flight callsign
(e.g. THY1017/TK1017), never by aircraft registration or ICAO24. Live CPA remains
the primary detector; route history is a veto layer used to avoid straight-line
false positives when a scheduled arrival normally turns for its destination.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable
from urllib.parse import quote

from app.aircraft.providers import get_http_client
from app.config import settings
from app.database import get_db
from app.intelligence.trajectory import HistorySample, haversine_km

logger = logging.getLogger(__name__)

KNOTS_TO_KM_S = 0.0005144444444444444
_CALLSIGN_RE = re.compile(r"[^A-Z0-9]")


@dataclass(slots=True, frozen=True)
class RoutePoint:
    latitude: float
    longitude: float
    timestamp: float = 0.0


@dataclass(slots=True, frozen=True)
class AirportInfo:
    icao: str = ""
    iata: str = ""
    name: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @property
    def code(self) -> str:
        return self.iata or self.icao or self.name or "unknown"


@dataclass(slots=True, frozen=True)
class FlightRouteInfo:
    callsign: str
    airport_codes: str = "unknown"
    plausible: bool = False
    origin: AirportInfo | None = None
    destination: AirportInfo | None = None


@dataclass(slots=True, frozen=True)
class RouteGateResult:
    suppress_alert: bool
    callsign: str
    reason: str
    history_days: int = 0
    similar_days: int = 0
    similarity_km: float | None = None
    destination_code: str = ""
    destination_distance_km: float | None = None
    expected_turn_pending: bool = False
    route_plausible: bool = False


def normalize_flight_key(callsign: str | None) -> str:
    """Return a stable commercial-flight key, not an aircraft identity."""
    key = _CALLSIGN_RE.sub("", (callsign or "").upper())
    if len(key) < 3 or not any(ch.isalpha() for ch in key) or not any(ch.isdigit() for ch in key):
        return ""
    return key[:16]


def _point(value: Any) -> RoutePoint | None:
    if isinstance(value, RoutePoint):
        return value
    if isinstance(value, HistorySample):
        return RoutePoint(float(value.latitude), float(value.longitude), float(value.timestamp))
    if isinstance(value, dict):
        try:
            lat = value.get("lat", value.get("latitude"))
            lon = value.get("lon", value.get("longitude"))
            return RoutePoint(float(lat), float(lon), float(value.get("t", value.get("timestamp", 0.0)) or 0.0))
        except (TypeError, ValueError):
            return None
    try:
        lat, lon = value[0], value[1]
        ts = value[2] if len(value) > 2 else 0.0
        return RoutePoint(float(lat), float(lon), float(ts or 0.0))
    except (TypeError, ValueError, IndexError):
        return None


def _clean_points(values: Iterable[Any], max_points: int = 160) -> list[RoutePoint]:
    points = [p for p in (_point(v) for v in values) if p is not None]
    if len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def _directed_similarity_km(a: list[RoutePoint], b: list[RoutePoint]) -> float | None:
    if len(a) < 2 or len(b) < 2:
        return None
    distances = [
        min(haversine_km(p.latitude, p.longitude, q.latitude, q.longitude) for q in b)
        for p in a
    ]
    return median(distances) if distances else None


def route_similarity_km(a: Iterable[Any], b: Iterable[Any]) -> float | None:
    """Robust symmetric route distance; smaller means more similar."""
    pa, pb = _clean_points(a), _clean_points(b)
    ab = _directed_similarity_km(pa, pb)
    ba = _directed_similarity_km(pb, pa)
    if ab is None or ba is None:
        return None
    return max(ab, ba)


def _current_to_history_similarity_km(current: Iterable[Any], historical: Iterable[Any]) -> float | None:
    """Compare only the observed current prefix to a complete historical path."""
    cur, hist = _clean_points(current), _clean_points(historical)
    return _directed_similarity_km(cur, hist)


def _minimum_distance_km(path: Iterable[Any], lat: float, lon: float) -> float | None:
    pts = _clean_points(path)
    if not pts:
        return None
    return min(haversine_km(p.latitude, p.longitude, lat, lon) for p in pts)


def _pairwise_history_similarity(routes: list[list[RoutePoint]]) -> list[float]:
    values: list[float] = []
    for i, left in enumerate(routes):
        for right in routes[i + 1 :]:
            value = route_similarity_km(left, right)
            if value is not None:
                values.append(value)
    return values


def evaluate_route_gate(
    *,
    callsign: str,
    current_path: Iterable[Any],
    historical_paths: Iterable[Iterable[Any]],
    observer_lat: float,
    observer_lon: float,
    alert_radius_km: float,
    destination: AirportInfo | None = None,
    route_plausible: bool = False,
    aircraft_lat: float | None = None,
    aircraft_lon: float | None = None,
    altitude_m: float | None = None,
    vertical_rate_mps: float | None = None,
    speed_kts: float | None = None,
    time_to_cpa_s: float | None = None,
) -> RouteGateResult:
    """Return a deterministic veto decision for a live CPA candidate.

    Route history may suppress an alert but is never allowed to create one.
    """
    key = normalize_flight_key(callsign)
    dest_code = destination.code if destination else ""
    if not key:
        return RouteGateResult(False, "", "no usable flight-number callsign", destination_code=dest_code)

    current = _clean_points(current_path)
    history = [_clean_points(route) for route in historical_paths]
    history = [route for route in history if len(route) >= 3]
    history_days = len(history)

    dest_distance: float | None = None
    if (
        route_plausible
        and destination
        and destination.latitude is not None
        and destination.longitude is not None
        and aircraft_lat is not None
        and aircraft_lon is not None
    ):
        dest_distance = haversine_km(
            float(aircraft_lat), float(aircraft_lon), float(destination.latitude), float(destination.longitude)
        )
        speed = float(speed_kts or 0.0) * KNOTS_TO_KM_S
        descending = (vertical_rate_mps is not None and vertical_rate_mps < -0.35) or (
            altitude_m is not None and altitude_m < 6500.0
        )
        if speed > 0.055 and descending and dest_distance <= 120.0 and time_to_cpa_s is not None and time_to_cpa_s > 0:
            eta_destination_s = dest_distance / speed
            if eta_destination_s + 75.0 < float(time_to_cpa_s):
                return RouteGateResult(
                    True,
                    key,
                    f"known destination {dest_code} is reached before projected observer CPA",
                    history_days=history_days,
                    destination_code=dest_code,
                    destination_distance_km=dest_distance,
                    expected_turn_pending=True,
                    route_plausible=True,
                )

    if history_days == 0:
        return RouteGateResult(
            False,
            key,
            "no previous flight-number route captured yet; live CPA remains authoritative",
            history_days=0,
            destination_code=dest_code,
            destination_distance_km=dest_distance,
            route_plausible=route_plausible,
        )

    pairwise = _pairwise_history_similarity(history)
    history_similarity = median(pairwise) if pairwise else None
    if history_similarity is not None and history_similarity > max(7.5, alert_radius_km * 0.45):
        return RouteGateResult(
            True,
            key,
            "last three flight-number routes are inconsistent",
            history_days=history_days,
            similarity_km=history_similarity,
            destination_code=dest_code,
            destination_distance_km=dest_distance,
            route_plausible=route_plausible,
        )

    current_sims = [
        value
        for route in history
        if (value := _current_to_history_similarity_km(current, route)) is not None
    ]
    current_similarity = median(current_sims) if current_sims else None
    similar_limit = max(5.0, alert_radius_km * 0.35)
    similar_days = sum(value <= similar_limit for value in current_sims)

    if len(current) >= 4 and current_similarity is not None and current_similarity > max(8.0, alert_radius_km * 0.55):
        return RouteGateResult(
            True,
            key,
            "today's route diverges from the recent flight-number pattern",
            history_days=history_days,
            similar_days=similar_days,
            similarity_km=current_similarity,
            destination_code=dest_code,
            destination_distance_km=dest_distance,
            route_plausible=route_plausible,
        )

    historical_minima = [
        value
        for route in history
        if (value := _minimum_distance_km(route, observer_lat, observer_lon)) is not None
    ]
    outside_margin = max(2.5, alert_radius_km * 0.18)
    all_historical_outside = bool(historical_minima) and all(
        value > alert_radius_km + outside_margin for value in historical_minima
    )

    required_similar_days = 1 if history_days == 1 else 2
    if all_historical_outside and similar_days >= required_similar_days:
        return RouteGateResult(
            True,
            key,
            "matching recent routes turn/stay outside the observer alert radius",
            history_days=history_days,
            similar_days=similar_days,
            similarity_km=current_similarity,
            destination_code=dest_code,
            destination_distance_km=dest_distance,
            expected_turn_pending=bool(destination and route_plausible),
            route_plausible=route_plausible,
        )

    return RouteGateResult(
        False,
        key,
        "route history does not veto live CPA",
        history_days=history_days,
        similar_days=similar_days,
        similarity_km=current_similarity,
        destination_code=dest_code,
        destination_distance_km=dest_distance,
        route_plausible=route_plausible,
    )


def _airport(raw: Any) -> AirportInfo | None:
    if not isinstance(raw, dict):
        return None
    try:
        lat = float(raw["lat"]) if raw.get("lat") is not None else None
        lon = float(raw["lon"]) if raw.get("lon") is not None else None
    except (TypeError, ValueError):
        lat = lon = None
    return AirportInfo(
        icao=str(raw.get("icao") or "").upper(),
        iata=str(raw.get("iata") or "").upper(),
        name=str(raw.get("name") or ""),
        latitude=lat,
        longitude=lon,
    )


def _route_info(callsign: str, payload: Any) -> FlightRouteInfo | None:
    """Parse either routeset's list shape or the single-route dict shape."""
    raw = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(raw, dict) or raw.get("airport_codes") in (None, "", "unknown"):
        return None
    airports = [_airport(item) for item in (raw.get("_airports") or [])]
    airports = [item for item in airports if item is not None]
    return FlightRouteInfo(
        callsign=callsign,
        airport_codes=str(raw.get("airport_codes") or "unknown"),
        plausible=bool(raw.get("plausible", False)),
        origin=airports[0] if airports else None,
        destination=airports[-1] if len(airports) >= 2 else None,
    )


class RouteHistoryService:
    """Persist and evaluate callsign-keyed flight routes with bounded storage."""

    def __init__(self) -> None:
        self._last_sample: dict[str, tuple[float, float, float]] = {}
        self._route_cache: dict[str, tuple[float, FlightRouteInfo | None]] = {}

    async def observe(self, ac: Any, *, now: float | None = None) -> None:
        key = normalize_flight_key(getattr(ac, "callsign", ""))
        if not key or getattr(ac, "latitude", None) is None or getattr(ac, "longitude", None) is None:
            return
        now = time.time() if now is None else now
        lat, lon = float(ac.latitude), float(ac.longitude)
        previous = self._last_sample.get(key)
        interval = max(15, int(settings.route_sample_interval_seconds))
        if previous:
            last_t, last_lat, last_lon = previous
            moved = haversine_km(last_lat, last_lon, lat, lon)
            if now - last_t < interval and moved < 1.5:
                return
        self._last_sample[key] = (now, lat, lon)
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        point = {
            "t": round(now, 1),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "altitude_m": getattr(ac, "altitude", None),
            "heading_deg": getattr(ac, "heading", None),
        }
        try:
            await get_db()["flight_route_samples"].update_one(
                {"callsign": key, "utc_date": day},
                {
                    "$set": {
                        "callsign": key,
                        "utc_date": day,
                        "updated_at": datetime.now(timezone.utc),
                        "expires_at": datetime.now(timezone.utc) + timedelta(days=8),
                    },
                    "$push": {"points": {"$each": [point], "$slice": -360}},
                },
                upsert=True,
            )
        except Exception:
            logger.exception("flight_route_observe_failed callsign=%s", key)

    async def _historical_paths(self, key: str, *, now: datetime | None = None) -> list[list[RoutePoint]]:
        now = now or datetime.now(timezone.utc)
        days = max(1, min(3, int(settings.route_history_days)))
        wanted = [(now.date() - timedelta(days=i)).isoformat() for i in range(1, days + 1)]
        cursor = get_db()["flight_route_samples"].find(
            {"callsign": key, "utc_date": {"$in": wanted}},
            {"points": 1, "utc_date": 1, "_id": 0},
        )
        docs = [doc async for doc in cursor]
        docs.sort(key=lambda doc: doc.get("utc_date", ""), reverse=True)
        return [_clean_points(doc.get("points") or []) for doc in docs if doc.get("points")]

    async def resolve_route(self, ac: Any) -> FlightRouteInfo | None:
        key = normalize_flight_key(getattr(ac, "callsign", ""))
        if not key or getattr(ac, "latitude", None) is None or getattr(ac, "longitude", None) is None:
            return None

        now = time.monotonic()
        cached = self._route_cache.get(key)
        if cached:
            ttl = max(60, int(settings.route_lookup_cache_seconds)) if cached[1] is not None else 60
            if now - cached[0] < ttl:
                return cached[1]

        client = await get_http_client()
        lat = float(ac.latitude)
        lon = float(ac.longitude)
        route: FlightRouteInfo | None = None

        # Preferred bulk endpoint. Railway has occasionally received a non-JSON
        # 200 response from it, so JSON decoding failure is intentionally recoverable.
        bulk_status: int | None = None
        try:
            response = await client.post(
                settings.route_lookup_url,
                json={"planes": [{"callsign": key, "lat": lat, "lng": lon}]},
            )
            bulk_status = response.status_code
            response.raise_for_status()
            route = _route_info(key, response.json())
        except Exception as exc:
            logger.info(
                "route_lookup_bulk_failed callsign=%s status=%s error=%s",
                key,
                bulk_status if bulk_status is not None else "na",
                type(exc).__name__,
            )

        # Official ADSB.lol single-route endpoint. It returns the same airport
        # metadata and calculates the plausible flag from the live position.
        if route is None:
            single_status: int | None = None
            try:
                url = (
                    f"{settings.route_lookup_single_url.rstrip('/')}"
                    f"/{quote(key, safe='')}/{lat:.5f}/{lon:.5f}"
                )
                response = await client.get(url)
                single_status = response.status_code
                response.raise_for_status()
                route = _route_info(key, response.json())
            except Exception as exc:
                logger.info(
                    "route_lookup_single_failed callsign=%s status=%s error=%s",
                    key,
                    single_status if single_status is not None else "na",
                    type(exc).__name__,
                )

        self._route_cache[key] = (now, route)
        return route

    async def evaluate(
        self,
        ac: Any,
        pred: Any,
        *,
        user_lat: float,
        user_lon: float,
        alert_radius_km: float,
        current_samples: Iterable[Any],
        notification_sent: bool = False,
    ) -> RouteGateResult:
        key = normalize_flight_key(getattr(ac, "callsign", ""))
        if not key:
            return RouteGateResult(False, "", "no usable flight-number callsign")
        try:
            history = await self._historical_paths(key)
        except Exception:
            logger.exception("flight_route_history_read_failed callsign=%s", key)
            history = []
        route = await self.resolve_route(ac)
        result = evaluate_route_gate(
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
        )
        logger.info(
            "route_gate callsign=%s suppress=%s history_days=%d similar_days=%d similarity_km=%s destination=%s expected_turn=%s reason=%s",
            result.callsign,
            result.suppress_alert,
            result.history_days,
            result.similar_days,
            f"{result.similarity_km:.2f}" if result.similarity_km is not None else "na",
            result.destination_code or "unknown",
            result.expected_turn_pending,
            result.reason,
        )
        return result


route_history_service = RouteHistoryService()
