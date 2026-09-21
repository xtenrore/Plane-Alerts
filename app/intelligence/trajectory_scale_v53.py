"""Plane Alerts v5.3 shared aircraft-motion projection.

This module keeps the v5.1 physical model unchanged while separating the
observer-independent part of trajectory projection from observer-specific CPA
and 3D evaluation. A bounded LRU reuses one projected aircraft motion path for
many users when they are evaluating the same accepted ADS-B history.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import math
import time
from typing import Iterable

from app.intelligence import trajectory as base
from app.intelligence.proximity3d_v51 import assess_proximity_3d

_MAX_CACHE_ENTRIES = 1024


@dataclass(slots=True, frozen=True)
class MotionPoint:
    seconds: float
    latitude: float
    longitude: float
    altitude_m: float | None
    heading_deg: float


@dataclass(slots=True)
class SharedMotionProjection:
    samples: list[base.HistorySample]
    speed_kts: float | None
    heading_deg: float | None
    heading_stability_deg: float | None
    speed_stability_kts: float | None
    turn_rate_deg_s: float
    acceleration_kts_s: float
    max_horizon_s: int
    step_s: int
    points: tuple[MotionPoint, ...]


_cache: OrderedDict[tuple, SharedMotionProjection] = OrderedDict()
_cache_hits = 0
_cache_misses = 0


def _sample_fingerprint(samples: list[base.HistorySample], max_horizon_s: int, step_s: int) -> tuple:
    """Return a compact immutable generation key for one accepted history.

    The history store only appends/replaces the latest canonical observation.
    Including the endpoints, latest kinematics and sample count is sufficient to
    invalidate the shared projection when that canonical history changes while
    keeping key construction O(1).
    """
    first = samples[0]
    latest = samples[-1]
    return (
        len(samples),
        round(first.timestamp, 3),
        round(latest.timestamp, 3),
        round(latest.latitude, 7),
        round(latest.longitude, 7),
        None if latest.altitude_m is None else round(float(latest.altitude_m), 2),
        None if latest.speed_kts is None else round(float(latest.speed_kts), 3),
        None if latest.heading_deg is None else round(float(latest.heading_deg) % 360.0, 3),
        None if latest.vertical_rate_mps is None else round(float(latest.vertical_rate_mps), 3),
        int(max_horizon_s),
        int(step_s),
    )


def _build_motion_projection(
    samples: Iterable[base.HistorySample],
    *,
    max_horizon_s: int,
    step_s: int,
) -> SharedMotionProjection:
    raw_samples = sorted(list(samples), key=lambda sample: sample.timestamp)
    if not raw_samples:
        raise ValueError("at least one trajectory sample is required")
    recent = base._contiguous_recent(raw_samples)
    latest = recent[-1]
    speed, heading = base._estimate_speed_heading(recent)
    headings = base._series_heading(recent[-12:])
    heading_spread = base._heading_spread(headings)
    speed_spread = base._speed_spread(recent[-12:])
    turn = base._turn_rate(recent[-12:])
    accel = base._acceleration(recent[-8:])

    points: list[MotionPoint] = []
    if speed is not None and heading is not None and speed >= 20:
        vertical_rate = latest.vertical_rate_mps or 0.0
        lat = latest.latitude
        lon = latest.longitude
        projected_heading = heading % 360.0
        altitude = latest.altitude_m
        projected_speed = float(speed)
        for seconds in range(0, int(max_horizon_s) + 1, int(step_s)):
            if seconds > 0:
                accel_factor = max(0.0, 1.0 - (seconds - step_s) / base._ACCEL_DECAY_S)
                projected_speed = max(20.0, projected_speed + accel * accel_factor * step_s)
                lat, lon = base.project_point(
                    lat,
                    lon,
                    projected_speed * base.KNOTS_TO_KM_S * step_s,
                    projected_heading,
                )
                turn_factor = max(0.0, 1.0 - (seconds - step_s) / base._TURN_DECAY_S)
                projected_heading = (projected_heading + turn * turn_factor * step_s) % 360.0
                if altitude is not None:
                    altitude = max(0.0, altitude + vertical_rate * step_s)
            points.append(
                MotionPoint(
                    float(seconds),
                    float(lat),
                    float(lon),
                    None if altitude is None else float(altitude),
                    float(projected_heading),
                )
            )

    return SharedMotionProjection(
        samples=recent,
        speed_kts=None if speed is None else float(speed),
        heading_deg=None if heading is None else float(heading),
        heading_stability_deg=heading_spread,
        speed_stability_kts=speed_spread,
        turn_rate_deg_s=float(turn),
        acceleration_kts_s=float(accel),
        max_horizon_s=int(max_horizon_s),
        step_s=int(step_s),
        points=tuple(points),
    )


def shared_motion_projection(
    samples: Iterable[base.HistorySample],
    *,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> SharedMotionProjection:
    """Return a bounded cached observer-independent motion projection."""
    global _cache_hits, _cache_misses
    raw_samples = sorted(list(samples), key=lambda sample: sample.timestamp)
    if not raw_samples:
        raise ValueError("at least one trajectory sample is required")
    recent = base._contiguous_recent(raw_samples)
    key = _sample_fingerprint(recent, max_horizon_s, step_s)
    cached = _cache.get(key)
    if cached is not None:
        _cache_hits += 1
        _cache.move_to_end(key)
        return cached
    _cache_misses += 1
    projection = _build_motion_projection(recent, max_horizon_s=max_horizon_s, step_s=step_s)
    _cache[key] = projection
    _cache.move_to_end(key)
    while len(_cache) > _MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)
    return projection


def motion_cache_snapshot() -> dict[str, int]:
    return {
        "entries": len(_cache),
        "max_entries": _MAX_CACHE_ENTRIES,
        "hits": _cache_hits,
        "misses": _cache_misses,
    }


def reset_motion_cache_for_tests() -> None:
    global _cache_hits, _cache_misses
    _cache.clear()
    _cache_hits = 0
    _cache_misses = 0


def predict_trajectory(
    samples: Iterable[base.HistorySample],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    *,
    now: float | None = None,
    user_altitude_m: float | None = None,
    max_horizon_s: int = 900,
    step_s: int = 3,
    altitude_relevance: bool = True,
) -> base.TrajectoryPrediction:
    """Evaluate the unchanged v5.1 model using shared absolute motion state."""
    motion = shared_motion_projection(samples, max_horizon_s=max_horizon_s, step_s=step_s)
    recent = motion.samples
    latest = recent[-1]
    now = time.time() if now is None else now
    age = max(float(latest.position_age_s or 0.0), now - latest.timestamp)
    stale = age > 30.0
    current = base.haversine_km(latest.latitude, latest.longitude, user_lat, user_lon)

    nominal_observer_altitude_m = float(user_altitude_m) if user_altitude_m is not None else 0.0
    current_slant = (
        math.hypot(current, abs(float(latest.altitude_m) - nominal_observer_altitude_m) / 1000.0)
        if latest.altitude_m is not None
        else None
    )
    trend = base._distance_trend(recent, user_lat, user_lon)
    speed = motion.speed_kts
    heading = motion.heading_deg
    heading_spread = motion.heading_stability_deg
    speed_spread = motion.speed_stability_kts
    turn = motion.turn_rate_deg_s
    accel = motion.acceleration_kts_s

    if stale or speed is None or heading is None or speed < 20:
        reason = "ADS-B position is stale" if stale else "insufficient speed/heading data"
        return base.TrajectoryPrediction(
            "Prediction uncertain",
            "Uncertain",
            0.15,
            current,
            current_slant,
            trend,
            current,
            current_slant,
            None,
            None,
            False,
            False,
            False,
            stale,
            turn,
            accel,
            heading_spread,
            speed_spread,
            reason,
            [],
        )

    speed_km_s = speed * base.KNOTS_TO_KM_S
    dynamic = int(current / max(speed_km_s, 1e-6) * 1.30 + 45)
    horizon = min(max_horizon_s, max(180, dynamic))
    path: list[base.ProjectedPoint] = []
    closest_h = current
    closest_slant = current_slant
    cpa_t = 0.0
    entry_t: float | None = 0.0 if current <= alert_radius_km else None

    for point in motion.points:
        if point.seconds > horizon:
            break
        horizontal = base.haversine_km(point.latitude, point.longitude, user_lat, user_lon)
        slant = (
            math.hypot(horizontal, abs(float(point.altitude_m) - nominal_observer_altitude_m) / 1000.0)
            if point.altitude_m is not None
            else horizontal
        )
        path.append(
            base.ProjectedPoint(
                point.seconds,
                point.latitude,
                point.longitude,
                horizontal,
                slant,
                point.altitude_m,
                point.heading_deg,
            )
        )
        if horizontal < closest_h:
            closest_h = horizontal
            closest_slant = slant
            cpa_t = point.seconds
        if entry_t is None and horizontal <= alert_radius_km:
            entry_t = point.seconds

    idx = min(range(len(path)), key=lambda index: path[index].horizontal_km)
    if 0 < idx < len(path) - 1:
        y1 = path[idx - 1].horizontal_km
        y2 = path[idx].horizontal_km
        y3 = path[idx + 1].horizontal_km
        denom = y1 - 2 * y2 + y3
        if abs(denom) > 1e-9:
            offset = base._clamp(0.5 * (y1 - y3) / denom, -1.0, 1.0)
            cpa_t = max(0.0, path[idx].seconds + offset * step_s)

    increasing = trend is not None and trend > 0.002
    already_passed = base._observed_passed(recent, user_lat, user_lon, alert_radius_km, trend)
    horizon_edge = cpa_t >= horizon - step_s
    horizontal_enters = (
        closest_h <= alert_radius_km
        and cpa_t > 0
        and not stale
        and not (horizon_edge and closest_h > alert_radius_km * 0.85)
    )
    turning_away = abs(turn) >= 0.15 and increasing and closest_h >= min(current, alert_radius_km * 1.1)

    proximity3d = assess_proximity_3d(
        path=path,
        samples=recent,
        alert_radius_km=alert_radius_km,
        age_s=age,
        observer_altitude_m=user_altitude_m,
        step_s=float(step_s),
    )
    altitude_suppressed = bool(
        altitude_relevance and horizontal_enters and proximity3d.suppress_horizontal_entry
    )
    enters = horizontal_enters and not altitude_suppressed

    score = (
        0.30
        + min(0.22, max(0, len(recent) - 1) * 0.035)
        + 0.16 * (1.0 - base._clamp(age / 20.0, 0.0, 1.0))
    )
    if heading_spread is not None:
        score += 0.14 * (1.0 - base._clamp(heading_spread / 18.0, 0.0, 1.0))
    if speed_spread is not None:
        score += 0.08 * (1.0 - base._clamp(speed_spread / 35.0, 0.0, 1.0))
    score -= (
        0.10 * base._clamp(abs(turn), 0.0, 1.0)
        + 0.06 * base._clamp(abs(accel) / 1.5, 0.0, 1.0)
        + 0.14 * base._clamp(cpa_t / max_horizon_s, 0.0, 1.0)
    )
    if len(recent) < 3:
        score -= 0.10
    if horizon_edge:
        score -= 0.10
    score = base._clamp(min(score, 0.25) if stale else score, 0.0, 0.98)
    confidence = (
        "High"
        if score >= 0.78
        else "Medium"
        if score >= 0.58
        else "Low"
        if score >= 0.36
        else "Uncertain"
    )

    if stale:
        state, reason = "Prediction uncertain", "ADS-B position is stale"
    elif already_passed:
        state, reason = (
            "Passed",
            "the aircraft was observed inside the configured radius and is now receding from its observed closest point",
        )
    elif turning_away:
        state, reason = (
            "Turning away",
            "sustained recent turn and distance trend move the aircraft away from the observer",
        )
    elif altitude_suppressed:
        state, reason = "Will not approach", proximity3d.reason
    elif enters and cpa_t <= 30:
        state, reason = (
            "Passing nearby",
            "robust projected path enters the configured radius and CPA is imminent",
        )
    elif enters:
        state, reason = "Approaching", "robust projected path enters the configured radius"
    elif increasing:
        state, reason = "Moving away", f"projected closest pass remains outside {alert_radius_km:.1f} km"
    else:
        state, reason = "Will not approach", f"projected closest pass remains outside {alert_radius_km:.1f} km"

    prediction = base.TrajectoryPrediction(
        state,
        confidence,
        score,
        current,
        current_slant,
        trend,
        closest_h,
        closest_slant,
        cpa_t,
        entry_t,
        enters,
        already_passed,
        turning_away,
        stale,
        turn,
        accel,
        heading_spread,
        speed_spread,
        reason,
        path,
    )
    prediction.current_vertical_separation_m = proximity3d.current_vertical_separation_m
    prediction.projected_closest_3d_km = proximity3d.projected_closest_3d_km
    prediction.projected_closest_3d_lower_bound_km = proximity3d.projected_closest_3d_lower_bound_km
    prediction.time_to_3d_cpa_s = proximity3d.time_to_3d_cpa_s
    prediction.horizontal_at_3d_cpa_km = proximity3d.horizontal_at_3d_cpa_km
    prediction.vertical_at_3d_cpa_m = proximity3d.vertical_at_3d_cpa_m
    prediction.three_d_available = proximity3d.available
    prediction.observer_altitude_known = proximity3d.observer_altitude_known
    prediction.altitude_confidence = proximity3d.altitude_confidence
    prediction.altitude_uncertainty_m = proximity3d.altitude_uncertainty_m
    prediction.altitude_relevance_applied = altitude_suppressed
    prediction.altitude_relevance_reason = proximity3d.reason
    return prediction
