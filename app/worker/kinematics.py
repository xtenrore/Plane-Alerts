"""Kinematic Physics Engine for High-Precision Trajectory Extrapolation.

Computes closed-form O(1) Napier spherical geodesy Closest Distance of Approach (CDA),
Estimated Time of Arrival (ETA), range-rate / closure rate, perimeter entry/exit times,
forward projections, and turn-rate arc geometry.
"""

from __future__ import annotations

import math
from typing import NamedTuple

# Earth's mean radius in kilometres
EARTH_RADIUS_KM = 6371.0

# Speed conversion constant: 1 knot = 0.000514444 km/s, 1 knot = 0.514444444 m/s
KNOTS_TO_KM_PER_SEC = 0.0005144444444444444
KNOTS_TO_MS = 0.5144444444444444
MS_TO_KM_PER_SEC = 0.001


class TrajectoryPoint(NamedTuple):
    """Point along a projected trajectory path."""

    seconds: int
    lat: float
    lon: float
    distance_to_user_km: float


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two coordinates."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    a = max(0.0, min(1.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))

    return EARTH_RADIUS_KM * c


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return initial bearing in degrees (0-360) from point 1 to point 2."""
    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlon = lon2_r - lon1_r
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)

    initial_bearing_rad = math.atan2(x, y)
    initial_bearing_deg = (math.degrees(initial_bearing_rad) + 360.0) % 360.0
    return initial_bearing_deg


def project_point(lat: float, lon: float, distance_km: float, bearing_deg: float) -> tuple[float, float]:
    """Project a point from (lat, lon) along a bearing by distance_km.

    Returns (new_lat, new_lon) in degrees.
    """
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    bearing_r = math.radians(bearing_deg)
    angular_dist = distance_km / EARTH_RADIUS_KM

    sin_new_lat = (
        math.sin(lat_r) * math.cos(angular_dist)
        + math.cos(lat_r) * math.sin(angular_dist) * math.cos(bearing_r)
    )
    sin_new_lat = max(-1.0, min(1.0, sin_new_lat))
    new_lat_r = math.asin(sin_new_lat)

    new_lon_r = lon_r + math.atan2(
        math.sin(bearing_r) * math.sin(angular_dist) * math.cos(lat_r),
        math.cos(angular_dist) - math.sin(lat_r) * math.sin(new_lat_r),
    )

    new_lat = math.degrees(new_lat_r)
    new_lon = (math.degrees(new_lon_r) + 540.0) % 360.0 - 180.0
    return new_lat, new_lon


def simulate_trajectory(
    start_lat: float,
    start_lon: float,
    speed_kts: float,
    heading_deg: float,
    turn_rate_deg_s: float,
    user_lat: float,
    user_lon: float,
    lookahead_seconds: int = 180,
    time_step_s: int = 2,
) -> list[TrajectoryPoint]:
    """Simulate aircraft trajectory path over lookahead_seconds.

    Applies turn_rate_deg_s to simulate banking curves or straight lines.
    Returns list of TrajectoryPoint entries sampled every time_step_s.
    """
    points: list[TrajectoryPoint] = []

    curr_lat = start_lat
    curr_lon = start_lon
    curr_heading = (heading_deg or 0.0) % 360.0
    speed_km_s = (speed_kts or 0.0) * KNOTS_TO_KM_PER_SEC

    # Initial point t = 0
    d_initial = haversine_distance_km(curr_lat, curr_lon, user_lat, user_lon)
    points.append(TrajectoryPoint(0, curr_lat, curr_lon, d_initial))

    for t in range(time_step_s, lookahead_seconds + 1, time_step_s):
        # Step distance in km
        step_dist_km = speed_km_s * time_step_s

        # Project position using current heading
        curr_lat, curr_lon = project_point(curr_lat, curr_lon, step_dist_km, curr_heading)

        # Update heading if turning
        if abs(turn_rate_deg_s) > 0.001:
            curr_heading = (curr_heading + turn_rate_deg_s * time_step_s) % 360.0

        # Calculate distance to user
        dist = haversine_distance_km(curr_lat, curr_lon, user_lat, user_lon)
        points.append(TrajectoryPoint(t, curr_lat, curr_lon, dist))

    return points


def calculate_cda_and_eta(
    start_lat: float,
    start_lon: float,
    speed_kts: float | None = None,
    heading_deg: float | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    radius_km: float = 15.0,
    lookahead_seconds: int = 180,
    buffer_km: float = 15.0,
    velocity_ms: float | None = None,
) -> dict:
    """Analytical O(1) Napier spherical geodesy CDA and ETA engine.

    Computes:
      - Continuous Closest Distance of Approach (CDA) on the spherical manifold.
      - Along-track distance and Time-to-CDA.
      - Exact perimeter entry and exit times (half-chord intersection).
      - Range-rate (relative closure rate) in m/s towards user.
      - Initial bearing from user to aircraft.
      - Classification: direct_hit, near_miss, grazing, receding, parallel, outside.
    """
    # 1. Resolve velocity and speed
    if velocity_ms is not None:
        v_ms = max(0.0, float(velocity_ms))
        v_kts = v_ms / KNOTS_TO_MS
    elif speed_kts is not None:
        v_kts = max(0.0, float(speed_kts))
        v_ms = v_kts * KNOTS_TO_MS
    else:
        v_kts = 0.0
        v_ms = 0.0

    v_km_s = v_ms * MS_TO_KM_PER_SEC

    # 2. Initial distance and bearings
    initial_dist = haversine_distance_km(start_lat, start_lon, user_lat, user_lon)

    if initial_dist < 1e-4:
        bearing_from_user = 0.0
        bearing_ac_to_user = 0.0
    else:
        bearing_from_user = initial_bearing(user_lat, user_lon, start_lat, start_lon)
        bearing_ac_to_user = initial_bearing(start_lat, start_lon, user_lat, user_lon)

    # 3. Handle missing heading
    if heading_deg is None:
        is_inside = initial_dist <= radius_km
        pass_type = "direct_hit" if (is_inside and initial_dist <= radius_km * 0.5) else ("near_miss" if is_inside else "outside")
        return {
            "should_notify": is_inside,
            "eta_seconds": 0.0 if is_inside else None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "cda_km": round(initial_dist, 2),
            "pass_type": pass_type,
            "trajectory_status": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": 0.0,
            "range_rate_mps": 0.0,
            "entry_seconds": 0.0 if is_inside else None,
            "exit_seconds": None,
            "along_track_km": 0.0,
            "initial_distance_km": round(initial_dist, 2),
            "reason": (
                f"Aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km), heading unavailable."
                if is_inside
                else "Heading unavailable; cannot extrapolate trajectory."
            ),
        }

    heading = heading_deg % 360.0

    # 4. Relative approach angle alpha
    rel_deg = ((bearing_ac_to_user - heading + 180.0) % 360.0) - 180.0
    rel_rad = math.radians(rel_deg)
    cos_rel = math.cos(rel_rad)
    sin_rel = math.sin(rel_rad)

    # Range rate / closure rate towards user (positive = closing)
    closure_rate_ms = v_ms * cos_rel

    # 5. Napier spherical right triangle calculations
    sigma = initial_dist / EARTH_RADIUS_KM
    sin_sigma = math.sin(sigma)
    cos_sigma = math.cos(sigma)

    # Cross-track distance (CDA)
    sin_cda = sin_sigma * abs(sin_rel)
    sin_cda = max(0.0, min(1.0, sin_cda))
    d_cda = EARTH_RADIUS_KM * math.asin(sin_cda)

    # Along-track distance to closest approach point
    d_at = EARTH_RADIUS_KM * math.atan2(sin_sigma * cos_rel, cos_sigma)

    # 6. Spherical perimeter entry/exit times
    if d_cda <= radius_km:
        cos_r = math.cos(radius_km / EARTH_RADIUS_KM)
        cos_cda = math.cos(d_cda / EARTH_RADIUS_KM)
        q = cos_r / cos_cda if cos_cda > 0.0 else 0.0
        q = max(-1.0, min(1.0, q))
        d_chord = EARTH_RADIUS_KM * math.acos(q)
        d_entry = d_at - d_chord
        d_exit = d_at + d_chord
    else:
        d_chord = 0.0
        d_entry = None
        d_exit = None

    # Time to CDA along forward path
    if v_km_s > 1e-6:
        t_cda = d_at / v_km_s
        t_entry = (d_entry / v_km_s) if d_entry is not None else None
        t_exit = (d_exit / v_km_s) if d_exit is not None else None
    else:
        t_cda = None
        t_entry = None
        t_exit = None

    # 7. Classification and Decision Logic
    is_inside = initial_dist <= radius_km

    if is_inside:
        should_notify = True
        eta_seconds = 0.0

        if initial_dist < 1e-4:
            pass_type = "direct_hit"
            reason = "Aircraft directly overhead."
            closest_pass = 0.0
            time_to_cda = 0.0
        elif v_km_s <= 1e-6:
            pass_type = "direct_hit" if initial_dist <= radius_km * 0.5 else "near_miss"
            reason = f"Stationary aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km)."
            closest_pass = initial_dist
            time_to_cda = None
        elif d_at <= 0.0 or cos_rel < -0.05:
            pass_type = "receding"
            reason = f"Aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km) and receding."
            closest_pass = initial_dist
            time_to_cda = None
        elif abs(cos_rel) <= 0.05:
            pass_type = "parallel"
            reason = f"Aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km) on tangential track."
            closest_pass = d_cda
            time_to_cda = round(t_cda, 1) if (t_cda is not None and t_cda > 0) else None
        else:
            closest_pass = d_cda
            time_to_cda = round(t_cda, 1) if (t_cda is not None and t_cda > 0) else None
            if d_cda <= max(1.0, radius_km * 0.5):
                pass_type = "direct_hit"
            elif d_cda <= radius_km * 0.8:
                pass_type = "near_miss"
            else:
                pass_type = "grazing"
            reason = f"Aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km)."

        return {
            "should_notify": should_notify,
            "eta_seconds": eta_seconds,
            "time_to_cda_seconds": time_to_cda,
            "closest_pass_km": round(closest_pass, 2),
            "cda_km": round(closest_pass, 2),
            "pass_type": pass_type,
            "trajectory_status": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": 0.0,
            "exit_seconds": round(t_exit, 1) if (t_exit is not None and t_exit > 0) else None,
            "along_track_km": round(d_at, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": reason,
        }

    # Aircraft is currently OUTSIDE radius (initial_dist > radius_km)
    if v_km_s <= 1e-6:
        # Stationary outside
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "cda_km": round(initial_dist, 2),
            "pass_type": "parallel",
            "trajectory_status": "parallel",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": 0.0,
            "range_rate_mps": 0.0,
            "entry_seconds": None,
            "exit_seconds": None,
            "along_track_km": 0.0,
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Stationary aircraft outside radius ({initial_dist:.1f}km > {radius_km:.1f}km).",
        }

    if d_at <= 0.0 or cos_rel < -0.05:
        # Receding away from observer
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "cda_km": round(initial_dist, 2),
            "pass_type": "receding",
            "trajectory_status": "receding",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": None,
            "exit_seconds": None,
            "along_track_km": round(d_at, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Aircraft is flying away from observer (distance {initial_dist:.1f}km).",
        }

    if abs(cos_rel) <= 0.05:
        # Parallel / tangential
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": round(t_cda, 1) if (t_cda is not None and t_cda > 0) else None,
            "closest_pass_km": round(d_cda, 2),
            "cda_km": round(d_cda, 2),
            "pass_type": "parallel",
            "trajectory_status": "parallel",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": None,
            "exit_seconds": None,
            "along_track_km": round(d_at, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Aircraft on tangential track passing at {d_cda:.1f}km.",
        }

    if d_cda > radius_km:
        # Approaching, but track misses perimeter
        pass_type = "grazing" if d_cda <= radius_km + buffer_km else "outside"
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": round(t_cda, 1) if (t_cda is not None and t_cda > 0) else None,
            "closest_pass_km": round(d_cda, 2),
            "cda_km": round(d_cda, 2),
            "pass_type": pass_type,
            "trajectory_status": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": None,
            "exit_seconds": None,
            "along_track_km": round(d_at, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Aircraft trajectory will pass at {d_cda:.2f}km (outside target radius {radius_km:.1f}km).",
        }

    # d_cda <= radius_km and d_at > 0 (track penetrates perimeter in the future)
    if d_cda <= max(1.0, radius_km * 0.5):
        pass_type = "direct_hit"
    elif d_cda <= radius_km * 0.8:
        pass_type = "near_miss"
    else:
        pass_type = "grazing"

    if t_entry is not None and 0.0 <= t_entry <= lookahead_seconds:
        should_notify = True
        eta_seconds = round(t_entry, 1)
        reason = f"Early warning: Predicted intercept in ~{t_entry:.0f}s (closest pass: {d_cda:.2f}km)."
    else:
        should_notify = False
        eta_seconds = None
        pass_type = "outside"
        t_entry_desc = f"{t_entry:.0f}s" if t_entry is not None else "never"
        reason = f"Aircraft will intercept in ~{t_entry_desc}, beyond lookahead window ({lookahead_seconds}s)."

    return {
        "should_notify": should_notify,
        "eta_seconds": eta_seconds,
        "time_to_cda_seconds": round(t_cda, 1) if (t_cda is not None and t_cda > 0) else None,
        "closest_pass_km": round(d_cda, 2),
        "cda_km": round(d_cda, 2),
        "pass_type": pass_type,
        "trajectory_status": pass_type,
        "bearing_from_user": round(bearing_from_user, 1),
        "closure_rate_ms": round(closure_rate_ms, 1),
        "range_rate_mps": round(closure_rate_ms, 1),
        "entry_seconds": round(t_entry, 1) if t_entry is not None else None,
        "exit_seconds": round(t_exit, 1) if t_exit is not None else None,
        "along_track_km": round(d_at, 2),
        "initial_distance_km": round(initial_dist, 2),
        "reason": reason,
    }


def evaluate_early_warning(
    start_lat: float,
    start_lon: float,
    speed_kts: float | None = None,
    heading_deg: float | None = None,
    turn_rate_deg_s: float = 0.0,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    radius_km: float = 15.0,
    lookahead_seconds: int = 180,
    buffer_km: float = 15.0,
    velocity_ms: float | None = None,
) -> dict:
    """Evaluate aircraft trajectory against user target location.

    When turn_rate_deg_s is negligible, executes in analytical O(1) time
    using Napier spherical geodesy. When turning, applies discrete simulation
    to project banking geometry.
    """
    # If turning is negligible (standard ADS-B / straight line path), use analytical O(1) engine
    if abs(turn_rate_deg_s) <= 0.05:
        return calculate_cda_and_eta(
            start_lat=start_lat,
            start_lon=start_lon,
            speed_kts=speed_kts,
            heading_deg=heading_deg,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=lookahead_seconds,
            buffer_km=buffer_km,
            velocity_ms=velocity_ms,
        )

    # Actively turning aircraft: simulate banking curve
    if velocity_ms is not None:
        v_ms = max(0.0, float(velocity_ms))
        v_kts = v_ms / KNOTS_TO_MS
    elif speed_kts is not None:
        v_kts = max(0.0, float(speed_kts))
        v_ms = v_kts * KNOTS_TO_MS
    else:
        v_kts = 0.0
        v_ms = 0.0

    initial_dist = haversine_distance_km(start_lat, start_lon, user_lat, user_lon)
    bearing_from_user = initial_bearing(user_lat, user_lon, start_lat, start_lon) if initial_dist > 1e-5 else 0.0
    bearing_ac_to_user = initial_bearing(start_lat, start_lon, user_lat, user_lon) if initial_dist > 1e-5 else 0.0
    heading = (heading_deg or 0.0) % 360.0
    rel_deg = ((bearing_ac_to_user - heading + 180.0) % 360.0) - 180.0
    closure_rate_ms = v_ms * math.cos(math.radians(rel_deg))

    points = simulate_trajectory(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=v_kts,
        heading_deg=heading,
        turn_rate_deg_s=turn_rate_deg_s,
        user_lat=user_lat,
        user_lon=user_lon,
        lookahead_seconds=lookahead_seconds,
        time_step_s=2,
    )

    cda_point = min(points, key=lambda p: p.distance_to_user_km)
    closest_dist = cda_point.distance_to_user_km
    # Allow 1km threshold for turning arc curvature tolerances
    turn_effective_radius = radius_km + 1.0
    entry_point = next((p for p in points if p.distance_to_user_km <= turn_effective_radius), None)

    if initial_dist <= radius_km:
        return {
            "should_notify": True,
            "eta_seconds": 0.0,
            "time_to_cda_seconds": float(cda_point.seconds) if cda_point.seconds > 0 else None,
            "closest_pass_km": round(closest_dist, 2),
            "cda_km": round(closest_dist, 2),
            "pass_type": "curve_intercept" if closest_dist <= radius_km * 0.5 else "near_miss",
            "trajectory_status": "curve_intercept" if closest_dist <= radius_km * 0.5 else "near_miss",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": 0.0,
            "exit_seconds": None,
            "along_track_km": round(initial_dist - closest_dist, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Aircraft currently inside radius ({initial_dist:.1f}km <= {radius_km:.1f}km).",
        }

    if entry_point is not None:
        return {
            "should_notify": True,
            "eta_seconds": float(entry_point.seconds),
            "time_to_cda_seconds": float(cda_point.seconds),
            "closest_pass_km": round(closest_dist, 2),
            "cda_km": round(closest_dist, 2),
            "pass_type": "curve_intercept",
            "trajectory_status": "curve_intercept",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "range_rate_mps": round(closure_rate_ms, 1),
            "entry_seconds": float(entry_point.seconds),
            "exit_seconds": None,
            "along_track_km": round(initial_dist - closest_dist, 2),
            "initial_distance_km": round(initial_dist, 2),
            "reason": f"Early warning: Predicted intercept in ~{entry_point.seconds}s (closest pass: {closest_dist:.2f}km).",
        }

    return {
        "should_notify": False,
        "eta_seconds": None,
        "time_to_cda_seconds": float(cda_point.seconds) if cda_point.seconds > 0 else None,
        "closest_pass_km": round(closest_dist, 2),
        "cda_km": round(closest_dist, 2),
        "pass_type": "grazing" if closest_dist <= radius_km + buffer_km else "outside",
        "trajectory_status": "grazing" if closest_dist <= radius_km + buffer_km else "outside",
        "bearing_from_user": round(bearing_from_user, 1),
        "closure_rate_ms": round(closure_rate_ms, 1),
        "range_rate_mps": round(closure_rate_ms, 1),
        "entry_seconds": None,
        "exit_seconds": None,
        "along_track_km": round(initial_dist - closest_dist, 2),
        "initial_distance_km": round(initial_dist, 2),
        "reason": f"Aircraft trajectory will pass at {closest_dist:.2f}km (outside target radius {radius_km:.1f}km).",
    }
