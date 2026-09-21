"""Shared observer-independent midpoint motion for Plane Alerts v5.3.

The v4.3 production wrapper owns midpoint integration. This module caches only
its absolute aircraft path (lat/lon/altitude/heading); observer-specific
distance, CPA, 3D relevance, confidence and lifecycle decisions remain outside
this cache and continue to run independently for every user.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from app.intelligence import trajectory as t

_MAX_CACHE_ENTRIES = 1024


@dataclass(slots=True, frozen=True)
class MidpointMotionPoint:
    seconds: float
    latitude: float
    longitude: float
    altitude_m: float | None
    heading_deg: float


_cache: OrderedDict[tuple, tuple[MidpointMotionPoint, ...]] = OrderedDict()
_hits = 0
_misses = 0


def _fingerprint(
    samples: list[t.HistorySample],
    *,
    speed_kts: float,
    heading_deg: float,
    acceleration_kts_s: float,
    turn_rate_deg_s: float,
    max_horizon_s: int,
    step_s: int,
) -> tuple:
    first = samples[0]
    latest = samples[-1]
    return (
        len(samples),
        round(first.timestamp, 3),
        round(latest.timestamp, 3),
        round(latest.latitude, 7),
        round(latest.longitude, 7),
        None if latest.altitude_m is None else round(float(latest.altitude_m), 2),
        None if latest.vertical_rate_mps is None else round(float(latest.vertical_rate_mps), 3),
        round(float(speed_kts), 4),
        round(float(heading_deg) % 360.0, 4),
        round(float(acceleration_kts_s), 5),
        round(float(turn_rate_deg_s), 5),
        int(max_horizon_s),
        int(step_s),
    )


def _midpoint_step(
    speed_kts: float,
    heading_deg: float,
    *,
    acceleration_kts_s: float,
    turn_rate_deg_s: float,
    acceleration_factor: float,
    turn_factor: float,
    step_s: float,
) -> tuple[float, float, float, float]:
    next_speed = max(
        20.0,
        float(speed_kts) + float(acceleration_kts_s) * float(acceleration_factor) * float(step_s),
    )
    next_heading = (
        float(heading_deg) + float(turn_rate_deg_s) * float(turn_factor) * float(step_s)
    ) % 360.0
    midpoint_speed = (float(speed_kts) + next_speed) / 2.0
    midpoint_heading = (
        float(heading_deg) + 0.5 * t._angle_delta(next_heading, float(heading_deg))
    ) % 360.0
    return next_speed, next_heading, midpoint_speed, midpoint_heading


def shared_midpoint_motion(
    samples: Iterable[t.HistorySample],
    *,
    speed_kts: float,
    heading_deg: float,
    acceleration_kts_s: float,
    turn_rate_deg_s: float,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> tuple[MidpointMotionPoint, ...]:
    """Return the exact v4.3 midpoint absolute path from a bounded LRU."""
    global _hits, _misses
    raw = sorted(list(samples), key=lambda sample: sample.timestamp)
    if not raw:
        raise ValueError("at least one trajectory sample is required")
    recent = t._contiguous_recent(raw)
    key = _fingerprint(
        recent,
        speed_kts=speed_kts,
        heading_deg=heading_deg,
        acceleration_kts_s=acceleration_kts_s,
        turn_rate_deg_s=turn_rate_deg_s,
        max_horizon_s=max_horizon_s,
        step_s=step_s,
    )
    cached = _cache.get(key)
    if cached is not None:
        _hits += 1
        _cache.move_to_end(key)
        return cached

    _misses += 1
    latest = recent[-1]
    vertical_rate = latest.vertical_rate_mps or 0.0
    lat = latest.latitude
    lon = latest.longitude
    projected_heading = float(heading_deg) % 360.0
    projected_speed = float(speed_kts)
    altitude = latest.altitude_m
    points: list[MidpointMotionPoint] = []

    for seconds in range(0, int(max_horizon_s) + 1, int(step_s)):
        if seconds > 0:
            acceleration_factor = max(0.0, 1.0 - (seconds - step_s) / t._ACCEL_DECAY_S)
            turn_factor = max(0.0, 1.0 - (seconds - step_s) / t._TURN_DECAY_S)
            next_speed, next_heading, midpoint_speed, midpoint_heading = _midpoint_step(
                projected_speed,
                projected_heading,
                acceleration_kts_s=acceleration_kts_s,
                turn_rate_deg_s=turn_rate_deg_s,
                acceleration_factor=acceleration_factor,
                turn_factor=turn_factor,
                step_s=float(step_s),
            )
            lat, lon = t.project_point(
                lat,
                lon,
                midpoint_speed * t.KNOTS_TO_KM_S * step_s,
                midpoint_heading,
            )
            projected_speed = next_speed
            projected_heading = next_heading
            if altitude is not None:
                altitude = max(0.0, altitude + vertical_rate * step_s)
        points.append(
            MidpointMotionPoint(
                seconds=float(seconds),
                latitude=float(lat),
                longitude=float(lon),
                altitude_m=None if altitude is None else float(altitude),
                heading_deg=float(projected_heading),
            )
        )

    result = tuple(points)
    _cache[key] = result
    _cache.move_to_end(key)
    while len(_cache) > _MAX_CACHE_ENTRIES:
        _cache.popitem(last=False)
    return result


def midpoint_cache_snapshot() -> dict[str, int]:
    return {
        "entries": len(_cache),
        "max_entries": _MAX_CACHE_ENTRIES,
        "hits": _hits,
        "misses": _misses,
    }


def reset_midpoint_cache_for_tests() -> None:
    global _hits, _misses
    _cache.clear()
    _hits = 0
    _misses = 0
