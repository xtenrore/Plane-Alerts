"""Deterministic route-history replay helpers for the Next 60 Minutes shadow forecast.

These helpers normalize historical route points from every supported legacy
encoding, require demonstrated local transit before a route can train a shadow
forecast, and aggregate repeat times safely across UTC midnight.

They are intentionally independent of live alert qualification. Historical
replays remain shadow-only evidence and missing ADS-B coverage is never treated
as proof of a pass or a miss.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

HISTORY_DAYS = 7


def parse_point_timestamp(value: Any) -> float | None:
    """Normalize a route-point timestamp to epoch seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            return dt.timestamp()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    if number > 10_000_000_000.0:
        number /= 1000.0
    return number


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def point_coordinates(point: dict[str, Any]) -> tuple[float, float] | None:
    try:
        lat = float(point.get("lat", point.get("latitude")))
        lon = float(point.get("lon", point.get("longitude")))
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def route_points(points: list[Any]) -> list[tuple[float, float, float]]:
    """Return valid ``(lat, lon, epoch_seconds)`` points in time order."""
    rows: list[tuple[float, float, float]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        coords = point_coordinates(point)
        timestamp = parse_point_timestamp(point.get("t", point.get("timestamp")))
        if coords is None or timestamp is None:
            continue
        rows.append((coords[0], coords[1], timestamp))
    rows.sort(key=lambda row: row[2])
    return rows


def local_pass_profile(
    points: list[tuple[float, float, float]],
    observer_lat: float,
    observer_lon: float,
    radius_km: float,
) -> dict[str, Any] | None:
    """Describe a demonstrated local transit, or return ``None`` if inconclusive.

    A shadow forecast needs evidence on both sides of closest approach. A route
    that starts/ends at CPA, only grazes the area with one fix, or never moves
    away again is not promoted to a repeatable pass. That is deliberately
    conservative: incomplete coverage is treated as inconclusive, not as proof
    of an arrival or a successful pass.
    """
    if len(points) < 3:
        return None
    radius = max(0.5, float(radius_km))
    near_ring = radius * 1.5
    distances = [
        haversine_km(observer_lat, observer_lon, lat, lon)
        for lat, lon, _ in points
    ]
    min_index = min(range(len(distances)), key=distances.__getitem__)
    closest_km = float(distances[min_index])
    if closest_km > radius:
        return None
    if min_index == 0 or min_index == len(points) - 1:
        return None
    if sum(1 for distance in distances if distance <= near_ring) < 2:
        return None

    before = distances[:min_index]
    after = distances[min_index + 1 :]
    separation = max(2.0, min(6.0, radius * 0.20))
    if max(before) < closest_km + separation or max(after) < closest_km + separation:
        return None

    return {
        "closest_km": closest_km,
        "closest_ts": float(points[min_index][2]),
        "points_inside_ring": sum(1 for distance in distances if distance <= near_ring),
    }


def circular_time_center(values: list[float]) -> tuple[float, float]:
    """Return circular center and maximum circular spread for seconds-of-day."""
    if not values:
        raise ValueError("at least one time-of-day value is required")
    normalized = [float(value) % 86400.0 for value in values]
    angles = [value / 86400.0 * math.tau for value in normalized]
    mean_sin = statistics.fmean(math.sin(angle) for angle in angles)
    mean_cos = statistics.fmean(math.cos(angle) for angle in angles)
    if math.hypot(mean_sin, mean_cos) < 1e-9:
        center = float(statistics.median(normalized))
    else:
        center = (math.atan2(mean_sin, mean_cos) % math.tau) / math.tau * 86400.0
    spread = max(min(abs(value - center), 86400.0 - abs(value - center)) for value in normalized)
    return center % 86400.0, float(spread)


# Compatibility name retained for callers/tests that historically used this helper.
def median_time_of_day(values: list[float]) -> tuple[float, float]:
    return circular_time_center(values)


def history_confidence(
    days: int,
    spread_s: float,
    horizon_s: float,
    distances: list[float],
    radius_km: float,
) -> str:
    if horizon_s > 1800.0:
        return "Low"
    median_closest = float(statistics.median(distances)) if distances else float(radius_km)
    if days >= 3 and spread_s <= 600.0 and median_closest <= float(radius_km) * 0.8:
        return "High"
    if days >= 2 and spread_s <= 900.0:
        return "Medium"
    return "Low"


def history_day_keys(now: datetime, history_days: int = HISTORY_DAYS) -> list[str]:
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return [
        (current.date() - timedelta(days=offset)).isoformat()
        for offset in range(0, max(0, int(history_days)) + 1)
    ]


def utc_date_key(route: dict[str, Any]) -> str:
    raw = route.get("utc_date")
    if isinstance(raw, datetime):
        dt = raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw.astimezone(timezone.utc)
        return dt.date().isoformat()
    return str(raw or "").strip()[:10]


def _next_occurrence(now: datetime, seconds_of_day: float) -> datetime:
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    midnight = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
    predicted = midnight + timedelta(seconds=float(seconds_of_day) % 86400.0)
    if predicted < current:
        predicted += timedelta(days=1)
    return predicted


def build_history_doc(
    callsign: str,
    routes: list[dict[str, Any]],
    observer_lat: float,
    observer_lon: float,
    radius_km: float,
    now: datetime,
    *,
    max_horizon_s: float = 3600.0,
) -> dict[str, Any] | None:
    """Build one conservative shadow forecast from repeated demonstrated passes."""
    pass_times: list[float] = []
    closest_distances: list[float] = []
    used_days: set[str] = set()
    aircraft_type = ""
    rejected_inconclusive = 0

    for route in routes:
        day = utc_date_key(route)
        if not day or day in used_days:
            continue
        profile = local_pass_profile(
            route_points(list(route.get("points") or [])),
            float(observer_lat),
            float(observer_lon),
            float(radius_km),
        )
        if profile is None:
            rejected_inconclusive += 1
            continue
        used_days.add(day)
        dt = datetime.fromtimestamp(float(profile["closest_ts"]), timezone.utc)
        pass_times.append(
            dt.hour * 3600.0
            + dt.minute * 60.0
            + dt.second
            + dt.microsecond / 1_000_000.0
        )
        closest_distances.append(float(profile["closest_km"]))
        aircraft_type = aircraft_type or str(route.get("aircraft_type") or "").strip().upper()

    if len(pass_times) < 2:
        return None
    predicted_sod, spread_s = circular_time_center(pass_times)
    predicted_at = _next_occurrence(now, predicted_sod)
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    horizon_s = (predicted_at - current).total_seconds()
    if horizon_s < 0.0 or horizon_s > float(max_horizon_s):
        return None

    confidence = history_confidence(
        len(pass_times), spread_s, horizon_s, closest_distances, float(radius_km)
    )
    if confidence == "Low":
        return None

    half_window = max(600.0, min(1800.0, spread_s + 300.0))
    return {
        "callsign": str(callsign or "").strip().upper(),
        "aircraft_type": aircraft_type,
        "predicted_cpa_at": predicted_at,
        "window_start": predicted_at - timedelta(seconds=half_window),
        "window_end": predicted_at + timedelta(seconds=half_window),
        "prediction_horizon_s": round(horizon_s, 1),
        "predicted_closest_km": round(float(statistics.median(closest_distances)), 3),
        "historical_days": len(pass_times),
        "historical_time_spread_s": round(spread_s, 1),
        "inconclusive_route_days_filtered": rejected_inconclusive,
        "confidence": confidence,
        "source": "history",
    }
