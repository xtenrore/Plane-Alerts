"""Numerical trajectory hotfixes backed by Prediction Lab evidence.

v4.2's predictor applies each acceleration increment at the start of a 3-second
projection step. That biases travelled distance and therefore CPA/ETA. Keep the
existing robust sample filtering/confidence model, but recompute the projected
path with midpoint (second-order) speed/heading integration.
"""
from __future__ import annotations

from dataclasses import replace
import math
import time
from typing import Iterable

from app.intelligence import trajectory as t

_ORIGINAL_PREDICT = t.predict_trajectory
_INSTALLED = False


def _midpoint_motion_step(
    speed_kts: float,
    heading_deg: float,
    *,
    acceleration_kts_s: float,
    turn_rate_deg_s: float,
    acceleration_factor: float,
    turn_factor: float,
    step_s: float,
) -> tuple[float, float, float, float]:
    """Return next speed/heading plus midpoint speed/heading for one step."""
    next_speed = max(
        20.0,
        float(speed_kts)
        + float(acceleration_kts_s) * float(acceleration_factor) * float(step_s),
    )
    next_heading = (
        float(heading_deg)
        + float(turn_rate_deg_s) * float(turn_factor) * float(step_s)
    ) % 360.0
    midpoint_speed = (float(speed_kts) + next_speed) / 2.0
    midpoint_heading = (
        float(heading_deg)
        + 0.5 * t._angle_delta(next_heading, float(heading_deg))
    ) % 360.0
    return next_speed, next_heading, midpoint_speed, midpoint_heading


def predict_trajectory_v43(
    samples: Iterable[t.HistorySample],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    *,
    now: float | None = None,
    user_altitude_m: float = 0.0,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> t.TrajectoryPrediction:
    raw_samples = sorted(list(samples), key=lambda sample: sample.timestamp)
    if not raw_samples:
        raise ValueError("at least one trajectory sample is required")

    effective_now = time.time() if now is None else now
    base = _ORIGINAL_PREDICT(
        raw_samples,
        user_lat,
        user_lon,
        alert_radius_km,
        now=effective_now,
        user_altitude_m=user_altitude_m,
        max_horizon_s=max_horizon_s,
        step_s=step_s,
    )
    recent = t._contiguous_recent(raw_samples)
    if base.stale or not recent:
        return base

    latest = recent[-1]
    speed, heading = t._estimate_speed_heading(recent)
    if speed is None or heading is None or speed < 20:
        return base

    speed_km_s = float(speed) * t.KNOTS_TO_KM_S
    dynamic = int(base.current_distance_km / max(speed_km_s, 1e-6) * 1.30 + 45)
    horizon = min(max_horizon_s, max(180, dynamic))
    vr = latest.vertical_rate_mps or 0.0
    curr_lat = latest.latitude
    curr_lon = latest.longitude
    curr_heading = float(heading) % 360.0
    projected_speed = float(speed)
    altitude = latest.altitude_m

    path: list[t.ProjectedPoint] = []
    closest_h = base.current_distance_km
    closest_slant = base.current_slant_km
    cpa_t = 0.0
    entry_t: float | None = 0.0 if base.current_distance_km <= alert_radius_km else None

    for seconds in range(0, horizon + 1, step_s):
        if seconds > 0:
            accel_factor = max(
                0.0,
                1.0 - (seconds - step_s) / t._ACCEL_DECAY_S,
            )
            turn_factor = max(
                0.0,
                1.0 - (seconds - step_s) / t._TURN_DECAY_S,
            )
            (
                next_speed,
                next_heading,
                midpoint_speed,
                midpoint_heading,
            ) = _midpoint_motion_step(
                projected_speed,
                curr_heading,
                acceleration_kts_s=base.acceleration_kts_s,
                turn_rate_deg_s=base.turn_rate_deg_s,
                acceleration_factor=accel_factor,
                turn_factor=turn_factor,
                step_s=step_s,
            )
            curr_lat, curr_lon = t.project_point(
                curr_lat,
                curr_lon,
                midpoint_speed * t.KNOTS_TO_KM_S * step_s,
                midpoint_heading,
            )
            projected_speed = next_speed
            curr_heading = next_heading
            if altitude is not None:
                altitude = max(0.0, altitude + vr * step_s)

        horizontal = t.haversine_km(curr_lat, curr_lon, user_lat, user_lon)
        slant = (
            math.hypot(
                horizontal,
                max(0.0, altitude - user_altitude_m) / 1000.0,
            )
            if altitude is not None
            else horizontal
        )
        path.append(
            t.ProjectedPoint(
                float(seconds),
                curr_lat,
                curr_lon,
                horizontal,
                slant,
                altitude,
                curr_heading,
            )
        )
        if horizontal < closest_h:
            closest_h = horizontal
            closest_slant = slant
            cpa_t = float(seconds)
        if entry_t is None and horizontal <= alert_radius_km:
            entry_t = float(seconds)

    idx = min(range(len(path)), key=lambda index: path[index].horizontal_km)
    if 0 < idx < len(path) - 1:
        y1 = path[idx - 1].horizontal_km ** 2
        y2 = path[idx].horizontal_km ** 2
        y3 = path[idx + 1].horizontal_km ** 2
        denom = y1 - 2 * y2 + y3
        if abs(denom) > 1e-9:
            offset = t._clamp(0.5 * (y1 - y3) / denom, -1.0, 1.0)
            cpa_t = max(0.0, path[idx].seconds + offset * step_s)
            # Locally constant velocity makes squared range quadratic in time.
            # Refine range too: a narrow-radius pass can lie between samples.
            closest_h = math.sqrt(max(0.0, y2 - (y1 - y3) ** 2 / (8.0 * denom))) if denom > 0 else closest_h
            cpa_altitude = path[idx].altitude_m
            closest_slant = math.hypot(closest_h, max(0.0, (cpa_altitude or user_altitude_m) - user_altitude_m) / 1000.0)

    # Projected times originate at the observation, not at the poll. Cached
    # positions must not restart their countdown every five seconds.
    age = max(0.0, effective_now - latest.timestamp)
    cpa_t = max(0.0, cpa_t - age)
    if entry_t is not None:
        entry_t = max(0.0, entry_t - age)
    path = [replace(point, seconds=point.seconds - age) for point in path if point.seconds >= age]

    increasing = (
        base.distance_trend_km_s is not None
        and base.distance_trend_km_s > 0.002
    )
    already_passed = cpa_t <= step_s and increasing
    horizon_edge = cpa_t >= horizon - step_s
    enters = (
        closest_h <= alert_radius_km
        and cpa_t > 0
        and not base.stale
        and not (horizon_edge and closest_h > alert_radius_km * 0.85)
    )
    turning_away = (
        abs(base.turn_rate_deg_s) >= 0.15
        and increasing
        and closest_h >= min(base.current_distance_km, alert_radius_km * 1.1)
    )

    directly_inside = (
        not base.stale
        and base.current_distance_km <= float(alert_radius_km)
    )
    if directly_inside:
        # Preserve the pre-existing reliability guarantee: after an ADS-B gap,
        # a fresh observed position physically inside the requested radius is
        # stronger evidence than a projected CPA that is already behind us.
        enters = True
        already_passed = False
        turning_away = False
        entry_t = 0.0
        state = "Passing nearby"
        reason = "fresh ADS-B position is directly inside the configured alert radius"
    elif base.stale:
        state, reason = "Prediction uncertain", "ADS-B position is stale"
    elif turning_away:
        state = "Turning away"
        reason = "sustained recent turn and distance trend move the aircraft away from the observer"
    elif already_passed:
        state, reason = "Passed", "closest approach is behind the current position and distance is increasing"
    elif enters and cpa_t <= 30:
        state = "Passing nearby"
        reason = "robust midpoint-projected path enters the configured radius and CPA is imminent"
    elif enters:
        state = "Approaching"
        reason = "robust midpoint-projected path enters the configured radius"
    elif increasing:
        state = "Moving away"
        reason = f"projected closest pass remains outside {alert_radius_km:.1f} km"
    else:
        state = "Will not approach"
        reason = f"projected closest pass remains outside {alert_radius_km:.1f} km"

    return replace(
        base,
        state=state,
        projected_closest_km=closest_h,
        projected_closest_slant_km=closest_slant,
        time_to_cpa_s=cpa_t,
        radius_entry_s=entry_t,
        enters_alert_radius=enters,
        already_passed=already_passed,
        turning_away=turning_away,
        reason=reason,
        path=path,
    )


def install_trajectory_hotfix_v43() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    t.predict_trajectory = predict_trajectory_v43
    _INSTALLED = True
