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
from app.intelligence.midpoint_scale_v53 import shared_midpoint_motion

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
    user_altitude_m: float | None = None,
    altitude_relevance: bool = True,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> t.TrajectoryPrediction:
    raw_samples = sorted(list(samples), key=lambda sample: sample.timestamp)
    if not raw_samples:
        raise ValueError("at least one trajectory sample is required")

    effective_now = time.time() if now is None else now
    # Preserve an unknown observer elevation for the authoritative v5.1 3D
    # predictor. This legacy wrapper still needs a numeric reference only for
    # its historical slant-distance diagnostics, where sea level is the same
    # conservative nominal fallback used by the core trajectory path.
    legacy_observer_altitude_m = float(user_altitude_m) if user_altitude_m is not None else 0.0
    base = _ORIGINAL_PREDICT(
        raw_samples,
        user_lat,
        user_lon,
        alert_radius_km,
        now=effective_now,
        user_altitude_m=user_altitude_m,
        altitude_relevance=altitude_relevance,
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

    # v5.3 shares only the absolute midpoint-integrated motion path. The
    # observer-specific distance/CPA/slant/3D calculations below remain per-user
    # and therefore preserve every established v4.3+ safety rule.
    motion_points = shared_midpoint_motion(
        raw_samples,
        speed_kts=float(speed),
        heading_deg=float(heading),
        acceleration_kts_s=base.acceleration_kts_s,
        turn_rate_deg_s=base.turn_rate_deg_s,
        max_horizon_s=max_horizon_s,
        step_s=step_s,
    )

    path: list[t.ProjectedPoint] = []
    closest_h = base.current_distance_km
    closest_slant = base.current_slant_km
    cpa_t = 0.0
    entry_t: float | None = 0.0 if base.current_distance_km <= alert_radius_km else None

    for point in motion_points:
        if point.seconds > horizon:
            break
        horizontal = t.haversine_km(point.latitude, point.longitude, user_lat, user_lon)
        slant = (
            math.hypot(
                horizontal,
                max(0.0, float(point.altitude_m) - legacy_observer_altitude_m) / 1000.0,
            )
            if point.altitude_m is not None
            else horizontal
        )
        path.append(
            t.ProjectedPoint(
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
        y1 = path[idx - 1].horizontal_km ** 2
        y2 = path[idx].horizontal_km ** 2
        y3 = path[idx + 1].horizontal_km ** 2
        denom = y1 - 2 * y2 + y3
        if abs(denom) > 1e-9:
            offset = t._clamp(0.5 * (y1 - y3) / denom, -1.0, 1.0)
            cpa_t = max(0.0, path[idx].seconds + offset * step_s)
            closest_h = math.sqrt(max(0.0, y2 - (y1 - y3) ** 2 / (8.0 * denom))) if denom > 0 else closest_h
            cpa_altitude = path[idx].altitude_m
            closest_slant = math.hypot(
                closest_h,
                max(0.0, float(cpa_altitude if cpa_altitude is not None else legacy_observer_altitude_m) - legacy_observer_altitude_m) / 1000.0,
            )

    # Deterministic Napier spherical geodesy integration for linear flight:
    # Analytical O(1) continuous Great-Circle Closest Distance of Approach (CDA)
    # and perimeter entry ETA demonstrably remove discrete Euler step errors.
    is_linear_flight = abs(base.turn_rate_deg_s) < 0.05 and abs(base.acceleration_kts_s) < 0.01
    napier_applied = False
    if is_linear_flight:
        from app.worker.kinematics import calculate_cda_and_eta

        napier = calculate_cda_and_eta(
            aircraft_lat=latest.latitude,
            aircraft_lon=latest.longitude,
            speed_kts=float(speed),
            heading_deg=float(heading),
            user_lat=user_lat,
            user_lon=user_lon,
            alert_radius_km=alert_radius_km,
            lookahead_seconds=max_horizon_s,
        )
        time_to_cda_val = napier.get("raw_time_to_cda_seconds", napier["time_to_cda_seconds"])
        cda_km_val = napier.get("raw_closest_pass_km", napier["closest_pass_km"])
        eta_val = napier.get("raw_eta_seconds", napier["eta_seconds"])

        if time_to_cda_val is not None and 0.0 <= time_to_cda_val <= float(horizon):
            closest_h = float(cda_km_val)
            cpa_t = float(time_to_cda_val)
            if eta_val is not None:
                entry_t = float(eta_val)
            elif base.current_distance_km <= alert_radius_km:
                entry_t = 0.0
            else:
                entry_t = None
            napier_applied = True
            cpa_alt = path[idx].altitude_m if (path and idx < len(path) and path[idx].altitude_m is not None) else latest.altitude_m
            closest_slant = math.hypot(
                closest_h,
                max(0.0, float(cpa_alt if cpa_alt is not None else legacy_observer_altitude_m) - legacy_observer_altitude_m) / 1000.0,
            )

    # Match the authoritative base predictor: provider-reported position age is
    # real observation latency even when the local timestamp is current. Using
    # wall-clock age alone made midpoint ETA systematically late on delayed
    # provider samples (retired external agent seq 140).
    age = max(float(latest.position_age_s or 0.0), max(0.0, effective_now - latest.timestamp))
    cpa_t = max(0.0, cpa_t - age)
    if entry_t is not None:
        entry_t = max(0.0, entry_t - age)
    path = [replace(point, seconds=point.seconds - age) for point in path if point.seconds >= age]

    increasing = base.distance_trend_km_s is not None and base.distance_trend_km_s > 0.002
    # v5.1 deliberately requires an observed in-radius pass before classifying
    # an encounter as Passed. Never restore the older projected-CPA shortcut.
    already_passed = bool(base.already_passed)
    horizon_edge = cpa_t >= horizon - step_s
    horizontal_enters = (
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

    directly_inside = not base.stale and base.current_distance_km <= float(alert_radius_km)
    if directly_inside and not already_passed:
        horizontal_enters = True
        turning_away = False
        entry_t = 0.0

    # v4.3 owns the midpoint-adjusted production path, so recompute v5.1 3D
    # relevance against that exact path instead of overwriting the core result
    # with a horizontal-only legacy decision.
    proximity3d = t.assess_proximity_3d(
        path=path,
        samples=recent,
        alert_radius_km=alert_radius_km,
        age_s=age,
        observer_altitude_m=user_altitude_m,
        step_s=float(step_s),
    )

    # When analytical Napier geodesy is authoritative, align 3D proximity quantities
    # continuously so horizontal and 3D CPA maintain mathematical and physical consistency.
    proj_3d = proximity3d.projected_closest_3d_km
    lower_3d = proximity3d.projected_closest_3d_lower_bound_km
    time_3d = proximity3d.time_to_3d_cpa_s
    horiz_3d = proximity3d.horizontal_at_3d_cpa_km
    vert_3d = proximity3d.vertical_at_3d_cpa_m
    relevance_reason = proximity3d.reason
    suppress_3d = proximity3d.suppress_horizontal_entry

    if napier_applied and proximity3d.available and latest.altitude_m is not None:
        vr_mps = float(latest.vertical_rate_mps or 0.0)
        alt_at_cpa_m = float(latest.altitude_m) + vr_mps * (cpa_t + age)
        v_km_s = float(speed) * t.KNOTS_TO_KM_S
        v_mps = v_km_s * 1000.0

        if proximity3d.observer_altitude_known and user_altitude_m is not None:
            obs_alt = float(user_altitude_m)
            delta_z = alt_at_cpa_m - obs_alt
            if abs(vr_mps) < 0.1:
                t_3d = cpa_t
                h_3d = closest_h
                v_3d = abs(delta_z)
                c_3d = math.hypot(closest_h, v_3d / 1000.0)
            else:
                denom_v = v_mps ** 2 + vr_mps ** 2
                tau_s = -(delta_z * vr_mps) / denom_v if denom_v > 1e-6 else 0.0
                t_3d = max(0.0, cpa_t + tau_s)
                h_3d = math.hypot(closest_h, v_km_s * tau_s)
                v_3d = abs(delta_z + vr_mps * tau_s)
                c_3d = math.hypot(h_3d, v_3d / 1000.0)

            closest_slant = c_3d
            proj_3d = c_3d
            time_3d = t_3d
            horiz_3d = h_3d
            vert_3d = v_3d
            lower_v = max(0.0, abs(delta_z) - (proximity3d.altitude_uncertainty_m or 0.0))
            lower_3d = math.hypot(closest_h, lower_v / 1000.0)
        else:
            proj_3d = None
            time_3d = None
            horiz_3d = None
            vert_3d = None
            from app.intelligence.proximity3d_v51 import _vertical_lower_bound_m
            lower_v = _vertical_lower_bound_m(alt_at_cpa_m, None, proximity3d.altitude_uncertainty_m or 0.0)
            lower_3d = math.hypot(closest_h, lower_v / 1000.0)

        suppress_3d = bool(
            proximity3d.altitude_confidence in {"High", "Medium"}
            and lower_3d > float(alert_radius_km) + 0.05
        )
        if suppress_3d:
            relevance_reason = (
                f"conservative 3D CPA lower bound {lower_3d:.2f} km remains outside "
                f"the {float(alert_radius_km):.2f} km alert radius despite altitude uncertainty"
            )

    altitude_suppressed = bool(
        altitude_relevance
        and horizontal_enters
        and suppress_3d
    )
    enters = horizontal_enters and not altitude_suppressed

    if base.stale:
        state, reason = "Prediction uncertain", "ADS-B position is stale"
    elif already_passed:
        state = "Passed"
        reason = "the aircraft was observed inside the configured radius and is now receding from its observed closest point"
    elif turning_away:
        state = "Turning away"
        reason = "sustained recent turn and distance trend move the aircraft away from the observer"
    elif altitude_suppressed:
        state, reason = "Will not approach", relevance_reason
    elif directly_inside and enters:
        state = "Passing nearby"
        reason = "fresh ADS-B position is directly inside the configured alert radius"
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
        current_vertical_separation_m=proximity3d.current_vertical_separation_m,
        projected_closest_3d_km=proj_3d,
        projected_closest_3d_lower_bound_km=lower_3d,
        time_to_3d_cpa_s=time_3d,
        horizontal_at_3d_cpa_km=horiz_3d,
        vertical_at_3d_cpa_m=vert_3d,
        three_d_available=proximity3d.available,
        observer_altitude_known=proximity3d.observer_altitude_known,
        altitude_confidence=proximity3d.altitude_confidence,
        altitude_uncertainty_m=proximity3d.altitude_uncertainty_m,
        altitude_relevance_applied=altitude_suppressed,
        altitude_relevance_reason=relevance_reason,
    )


def install_trajectory_hotfix_v43() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    t.predict_trajectory = predict_trajectory_v43
    _INSTALLED = True
