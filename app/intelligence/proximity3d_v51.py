"""Plane Alerts v5.1 deterministic 3D proximity assessment.

This module never calls AI or network services. It consumes the already-built
projected trajectory and ADS-B altitude evidence, computes true three-dimensional
CPA when an observer elevation is known, and otherwise computes a conservative
lower bound across the full physically possible observer-elevation range.

Altitude may suppress a horizontal alert only when the evidence is trustworthy
and the conservative 3D lower bound is outside the configured radius. Missing,
malformed or discontinuous altitude always fails open to horizontal CPA.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

# Global terrestrial bounds deliberately wider than observed inhabited terrain.
# They allow safe lower-bound reasoning when a user's exact elevation is absent.
OBSERVER_ELEVATION_MIN_M = -600.0
OBSERVER_ELEVATION_MAX_M = 9000.0
AIRCRAFT_ALTITUDE_MIN_M = -1000.0
AIRCRAFT_ALTITUDE_MAX_M = 25000.0
MAX_PLAUSIBLE_VERTICAL_RATE_MPS = 120.0
BASE_BARO_UNCERTAINTY_M = 250.0


@dataclass(frozen=True, slots=True)
class Proximity3D:
    available: bool
    observer_altitude_known: bool
    current_vertical_separation_m: float | None
    projected_closest_3d_km: float | None
    projected_closest_3d_lower_bound_km: float | None
    time_to_3d_cpa_s: float | None
    horizontal_at_3d_cpa_km: float | None
    vertical_at_3d_cpa_m: float | None
    altitude_confidence: str
    altitude_uncertainty_m: float | None
    suppress_horizontal_entry: bool
    reason: str


def _number(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _valid_aircraft_altitude(value: Any) -> float | None:
    number = _number(value)
    if number is None or not AIRCRAFT_ALTITUDE_MIN_M <= number <= AIRCRAFT_ALTITUDE_MAX_M:
        return None
    return number


def _valid_observer_altitude(value: Any) -> float | None:
    number = _number(value)
    if number is None or not OBSERVER_ELEVATION_MIN_M <= number <= OBSERVER_ELEVATION_MAX_M:
        return None
    return number


def _altitude_quality(samples: Iterable[Any], *, age_s: float) -> tuple[bool, str, float, str]:
    recent = list(samples)[-8:]
    if not recent:
        return False, "Unavailable", 0.0, "no altitude history"
    latest_alt = _valid_aircraft_altitude(getattr(recent[-1], "altitude_m", None))
    if latest_alt is None:
        return False, "Unavailable", 0.0, "latest ADS-B altitude is missing or outside physical bounds"

    vr = _number(getattr(recent[-1], "vertical_rate_mps", None))
    if vr is not None and abs(vr) > MAX_PLAUSIBLE_VERTICAL_RATE_MPS:
        return False, "Unavailable", 0.0, "vertical-rate report is outside the v5.1 physical plausibility bound"

    usable: list[tuple[float, float]] = []
    for sample in recent:
        altitude = _valid_aircraft_altitude(getattr(sample, "altitude_m", None))
        timestamp = _number(getattr(sample, "timestamp", None))
        if altitude is not None and timestamp is not None:
            usable.append((timestamp, altitude))

    for (ta, aa), (tb, ab) in zip(usable, usable[1:]):
        dt = tb - ta
        if 0.25 < dt <= 30.0:
            # Generous enough for very high-performance aircraft, but rejects
            # provider unit swaps and multi-thousand-metre ADS-B jumps.
            allowed = 300.0 + MAX_PLAUSIBLE_VERTICAL_RATE_MPS * dt
            if abs(ab - aa) > allowed:
                return False, "Unavailable", 0.0, "recent altitude history contains an implausible discontinuity"

    uncertainty = BASE_BARO_UNCERTAINTY_M
    uncertainty += min(750.0, max(0.0, float(age_s)) * min(abs(vr or 0.0), 50.0))
    if len(usable) < 2:
        uncertainty += 450.0
    elif len(usable) < 3:
        uncertainty += 150.0

    if len(usable) >= 3 and age_s <= 10.0:
        confidence = "High"
    elif len(usable) >= 2 and age_s <= 20.0:
        confidence = "Medium"
    else:
        confidence = "Low"
    return True, confidence, uncertainty, "altitude evidence passed physical and continuity checks"


def _vertical_lower_bound_m(aircraft_altitude_m: float, observer_altitude_m: float | None, uncertainty_m: float) -> float:
    if observer_altitude_m is not None:
        separation = abs(aircraft_altitude_m - observer_altitude_m)
    elif aircraft_altitude_m < OBSERVER_ELEVATION_MIN_M:
        separation = OBSERVER_ELEVATION_MIN_M - aircraft_altitude_m
    elif aircraft_altitude_m > OBSERVER_ELEVATION_MAX_M:
        separation = aircraft_altitude_m - OBSERVER_ELEVATION_MAX_M
    else:
        separation = 0.0
    return max(0.0, separation - uncertainty_m)


def _parabolic_time(points: list[Any], idx: int, attr: str, step_s: float) -> float:
    if not 0 < idx < len(points) - 1:
        return float(getattr(points[idx], "seconds", 0.0))
    y1 = float(getattr(points[idx - 1], attr))
    y2 = float(getattr(points[idx], attr))
    y3 = float(getattr(points[idx + 1], attr))
    denom = y1 - 2.0 * y2 + y3
    if abs(denom) <= 1e-9:
        return float(getattr(points[idx], "seconds", 0.0))
    offset = max(-1.0, min(1.0, 0.5 * (y1 - y3) / denom))
    return max(0.0, float(getattr(points[idx], "seconds", 0.0)) + offset * step_s)


def assess_proximity_3d(
    *,
    path: Iterable[Any],
    samples: Iterable[Any],
    alert_radius_km: float,
    age_s: float,
    observer_altitude_m: float | None,
    step_s: float,
) -> Proximity3D:
    points = list(path)
    valid, confidence, uncertainty_m, quality_reason = _altitude_quality(samples, age_s=age_s)
    observer_alt = _valid_observer_altitude(observer_altitude_m)
    observer_known = observer_alt is not None
    if not valid or not points:
        return Proximity3D(
            False, observer_known, None, None, None, None, None, None,
            confidence, uncertainty_m if valid else None, False,
            f"3D relevance unavailable: {quality_reason}; horizontal CPA retained",
        )

    rows: list[tuple[Any, float, float, float | None]] = []
    exact_rows: list[tuple[Any, float, float]] = []
    for point in points:
        altitude = _valid_aircraft_altitude(getattr(point, "altitude_m", None))
        if altitude is None:
            continue
        horizontal = max(0.0, float(getattr(point, "horizontal_km", 0.0)))
        lower_vertical = _vertical_lower_bound_m(altitude, observer_alt, uncertainty_m)
        lower_slant = math.hypot(horizontal, lower_vertical / 1000.0)
        exact_slant = None
        if observer_known:
            exact_vertical = abs(altitude - observer_alt)
            exact_slant = math.hypot(horizontal, exact_vertical / 1000.0)
            exact_rows.append((point, exact_slant, exact_vertical))
        rows.append((point, lower_slant, lower_vertical, exact_slant))

    if not rows:
        return Proximity3D(
            False, observer_known, None, None, None, None, None, None,
            "Unavailable", None, False,
            "3D relevance unavailable: projected altitude is missing or malformed; horizontal CPA retained",
        )

    lower_point, lower_min, _, _ = min(rows, key=lambda item: item[1])
    exact_min = exact_t = exact_horizontal = exact_vertical = current_vertical = None
    if exact_rows:
        exact_idx = min(range(len(exact_rows)), key=lambda i: exact_rows[i][1])
        exact_point, exact_min, exact_vertical = exact_rows[exact_idx]
        exact_horizontal = float(getattr(exact_point, "horizontal_km", 0.0))
        exact_t = _parabolic_time([row[0] for row in exact_rows], exact_idx, "slant_km", float(step_s))
        first_alt = _valid_aircraft_altitude(getattr(points[0], "altitude_m", None))
        if first_alt is not None:
            current_vertical = abs(first_alt - observer_alt)

    # One altitude point is useful diagnostically but must never suppress.
    suppress = (
        confidence in {"High", "Medium"}
        and lower_min > float(alert_radius_km) + 0.05
    )
    if suppress:
        reason = (
            f"conservative 3D CPA lower bound {lower_min:.2f} km remains outside "
            f"the {float(alert_radius_km):.2f} km alert radius despite altitude uncertainty"
        )
    elif observer_known:
        reason = "true 3D CPA is available; altitude evidence does not safely exclude the horizontal approach"
    else:
        reason = (
            "observer elevation is unknown; v5.1 used a global terrain envelope and retained horizontal CPA "
            "unless the conservative 3D lower bound proved the pass outside the radius"
        )

    return Proximity3D(
        True,
        observer_known,
        current_vertical,
        exact_min,
        lower_min,
        exact_t,
        exact_horizontal,
        exact_vertical,
        confidence,
        uncertainty_m,
        suppress,
        reason,
    )
