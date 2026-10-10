"""Kinematic Physics Engine for High-Precision Trajectory Extrapolation.

Computes Haversine spherical distance, bearing, forward point projections,
turn-rate arc geometry, analytical O(1) Napier spherical geodesy Closest
Distance of Approach (CDA), and perimeter entry ETA.
"""

from __future__ import annotations

import math
from typing import Any, NamedTuple

# Earth's mean radius in kilometres
EARTH_RADIUS_KM = 6371.0

# Speed conversion constant: 1 knot = 0.000514444 km/s
KNOTS_TO_KM_PER_SEC = 0.0005144444444444444


class TrajectoryPoint(NamedTuple):
    """Point along a projected trajectory path."""

    seconds: int
    lat: float
    lon: float
    distance_to_user_km: float


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in kilometres between two coordinates.

    Applies safe floating-point clamping to guard against numerical overflow or
    roundoff domain errors (e.g. sqrt of negative numbers).
    """
    if abs(lat1 - lat2) < 1e-12 and abs(lon1 - lon2) < 1e-12:
        return 0.0

    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r

    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2.0) ** 2
    a_clamped = max(0.0, min(1.0, a))
    c = 2.0 * math.atan2(math.sqrt(a_clamped), math.sqrt(max(0.0, 1.0 - a_clamped)))

    return EARTH_RADIUS_KM * c


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return initial bearing in degrees (0-360) from point 1 to point 2."""
    if abs(lat1 - lat2) < 1e-12 and abs(lon1 - lon2) < 1e-12:
        return 0.0

    lat1_r, lon1_r = math.radians(lat1), math.radians(lon1)
    lat2_r, lon2_r = math.radians(lat2), math.radians(lon2)

    dlon = lon2_r - lon1_r
    x = math.sin(dlon) * math.cos(lat2_r)
    y = math.cos(lat1_r) * math.sin(lat2_r) - math.sin(lat1_r) * math.cos(lat2_r) * math.cos(dlon)

    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return 0.0

    initial_bearing_rad = math.atan2(x, y)
    initial_bearing_deg = (math.degrees(initial_bearing_rad) + 360.0) % 360.0
    return initial_bearing_deg


def project_point(lat: float, lon: float, distance_km: float, bearing_deg: float) -> tuple[float, float]:
    """Project a point from (lat, lon) along a bearing by distance_km.

    Returns (new_lat, new_lon) in degrees, with safe input clamping for asin.
    """
    if abs(distance_km) < 1e-12:
        return lat, lon

    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    bearing_r = math.radians(bearing_deg % 360.0)
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
    curr_heading = heading_deg % 360.0
    speed_km_s = speed_kts * KNOTS_TO_KM_PER_SEC

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
    aircraft_lat: float,
    aircraft_lon: float,
    speed_km_s: float | None = None,
    heading_deg: float | None = None,
    user_lat: float = 0.0,
    user_lon: float = 0.0,
    alert_radius_km: float | None = None,
    turn_rate_deg_s: float = 0.0,
    latency_s: float = 0.0,
    *,
    speed_kts: float | None = None,
    velocity_ms: float | None = None,
    radius_km: float | None = None,
    lookahead_seconds: int = 180,
) -> dict[str, Any]:
    """Analytical O(1) Napier spherical geodesy CDA and ETA engine.

    Calculates:
      - should_notify: bool
      - eta_seconds: float | None (perimeter entry time; None if already inside or never enters)
      - time_to_cda_seconds: float | None (time to closest approach; None if receding/stationary)
      - closest_pass_km: float (Closest Distance of Approach)
      - pass_type: "direct_hit" | "near_miss" | "grazing" | "receding" | "parallel" | "curve_intercept" | "outside"
      - bearing_from_user: float (0-360 degrees)
      - closure_rate_ms: float (rate of range closure in m/s)
      - reason: str
    """
    # Resolve speed in km/s
    if speed_km_s is not None:
        v_km_s = float(speed_km_s)
    elif speed_kts is not None:
        v_km_s = float(speed_kts) * KNOTS_TO_KM_PER_SEC
    elif velocity_ms is not None:
        v_km_s = float(velocity_ms) / 1000.0
    else:
        v_km_s = 0.0
    v_km_s = max(0.0, v_km_s)

    # Resolve radius
    r_km = alert_radius_km if alert_radius_km is not None else (radius_km if radius_km is not None else 10.0)
    r_km = max(0.1, float(r_km))

    # Advance initial position by observation latency if specified
    lat_curr = aircraft_lat
    lon_curr = aircraft_lon
    hdg = (float(heading_deg) % 360.0) if heading_deg is not None else 0.0

    if latency_s > 0.0 and v_km_s > 1e-6:
        if abs(turn_rate_deg_s) > 0.05:
            mid_hdg = (hdg + turn_rate_deg_s * latency_s * 0.5) % 360.0
            lat_curr, lon_curr = project_point(lat_curr, lon_curr, v_km_s * latency_s, mid_hdg)
            hdg = (hdg + turn_rate_deg_s * latency_s) % 360.0
        else:
            lat_curr, lon_curr = project_point(lat_curr, lon_curr, v_km_s * latency_s, hdg)

    initial_dist = haversine_distance_km(lat_curr, lon_curr, user_lat, user_lon)
    is_inside = initial_dist <= r_km

    # Observer-to-aircraft bearing
    if initial_dist < 1e-4:
        bearing_from_user = 0.0
        bearing_to_user = 0.0
    else:
        bearing_from_user = initial_bearing(user_lat, user_lon, lat_curr, lon_curr)
        bearing_to_user = initial_bearing(lat_curr, lon_curr, user_lat, user_lon)

    # Coincident coordinate special case (< 50m)
    if initial_dist < 0.05:
        return {
            "should_notify": True,
            "eta_seconds": None,
            "time_to_cda_seconds": 0.0,
            "closest_pass_km": 0.0,
            "pass_type": "direct_hit",
            "bearing_from_user": 0.0,
            "closure_rate_ms": 0.0,
            "reason": f"Aircraft directly overhead observer ({initial_dist:.2f}km).",
        }

    # Stationary aircraft special case (< ~0.04 km/h)
    if v_km_s <= 1e-6:
        pass_type = "direct_hit" if initial_dist <= 1.0 else ("near_miss" if is_inside else "outside")
        return {
            "should_notify": is_inside,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "pass_type": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": 0.0,
            "reason": f"Stationary aircraft at {initial_dist:.2f}km ({'inside' if is_inside else 'outside'} radius).",
        }

    # Missing heading: cannot project motion direction
    if heading_deg is None:
        return {
            "should_notify": is_inside,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "pass_type": "direct_hit" if initial_dist <= 1.0 else ("near_miss" if is_inside else "outside"),
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": 0.0,
            "reason": f"Missing heading telemetry; distance is {initial_dist:.2f}km.",
        }

    # Turning flight trajectory simulation
    if abs(turn_rate_deg_s) > 0.05:
        speed_kts_eff = v_km_s / KNOTS_TO_KM_PER_SEC
        points = simulate_trajectory(
            start_lat=lat_curr,
            start_lon=lon_curr,
            speed_kts=speed_kts_eff,
            heading_deg=hdg,
            turn_rate_deg_s=turn_rate_deg_s,
            user_lat=user_lat,
            user_lon=user_lon,
            lookahead_seconds=lookahead_seconds,
            time_step_s=2,
        )
        min_idx = min(range(len(points)), key=lambda i: points[i].distance_to_user_km)
        best_pt = points[min_idx]
        cda_refined = best_pt.distance_to_user_km
        time_to_cda = float(best_pt.seconds)

        # Parabolic refinement on d^2 around discrete minimum
        if 0 < min_idx < len(points) - 1:
            y1 = points[min_idx - 1].distance_to_user_km ** 2
            y2 = points[min_idx].distance_to_user_km ** 2
            y3 = points[min_idx + 1].distance_to_user_km ** 2
            denom = y1 - 2.0 * y2 + y3
            if denom > 1e-9:
                offset = max(-1.0, min(1.0, 0.5 * (y1 - y3) / denom))
                time_to_cda = max(0.0, best_pt.seconds + offset * 2.0)
                cda_refined = math.sqrt(max(0.0, y2 - (y1 - y3) ** 2 / (8.0 * denom)))

        entry_point = next((p for p in points if p.distance_to_user_km <= r_km), None)
        entry_eta = float(entry_point.seconds) if (entry_point and not is_inside) else None
        should_notify = is_inside or (entry_point is not None and entry_point.seconds <= lookahead_seconds)
        pass_type = "curve_intercept" if should_notify else "outside"

        rel_angle_deg = ((bearing_to_user - hdg + 180.0) % 360.0) - 180.0
        closure_rate_ms = v_km_s * 1000.0 * math.cos(math.radians(rel_angle_deg))

        return {
            "should_notify": should_notify,
            "eta_seconds": entry_eta,
            "time_to_cda_seconds": round(time_to_cda, 1) if (time_to_cda > 0 or is_inside) else None,
            "closest_pass_km": round(cda_refined, 2),
            "pass_type": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "reason": f"Turning trajectory ({turn_rate_deg_s:+.1f}°/s); CDA: {cda_refined:.2f}km in ~{time_to_cda:.0f}s.",
        }

    # ── Linear Great-Circle Napier Spherical Geodesy Engine ───────────────────
    # Relative bearing angle from aircraft motion track to observer
    rel_angle_deg = ((bearing_to_user - hdg + 180.0) % 360.0) - 180.0
    rel_angle_rad = math.radians(rel_angle_deg)
    cos_rel = math.cos(rel_angle_rad)
    sin_rel = math.sin(rel_angle_rad)

    # Central angular distance sigma_0 = d / R
    sigma_0 = initial_dist / EARTH_RADIUS_KM
    sin_sigma = math.sin(sigma_0)
    cos_sigma = math.cos(sigma_0)

    # Napier Cross-Track Distance (CDA): sin(sigma_xt) = sin(sigma_0) * |sin(rel_angle)|
    sin_xt = max(-1.0, min(1.0, sin_sigma * abs(sin_rel)))
    sigma_xt = math.asin(sin_xt)
    cda_km = EARTH_RADIUS_KM * sigma_xt

    # Along-Track Distance: tan(sigma_at) = tan(sigma_0) * cos(rel_angle)
    # Using atan2 to safely preserve sign and handle all quadrants
    sigma_at = math.atan2(sin_sigma * cos_rel, cos_sigma)
    d_at = EARTH_RADIUS_KM * sigma_at

    # Range closure rate (m/s)
    closure_rate_ms = v_km_s * 1000.0 * cos_rel

    # Spherical chord perimeter intersection for alert radius r_km
    # In right spherical triangle: cos(r/R) = cos(cda/R) * cos(chord/R)
    cos_r = math.cos(r_km / EARTH_RADIUS_KM)
    cos_cda = math.cos(cda_km / EARTH_RADIUS_KM)
    intersects_radius = False
    d_entry: float | None = None
    d_exit: float | None = None

    if cos_cda > 1e-9 and cda_km <= r_km:
        q = cos_r / cos_cda
        if q <= 1.0:
            intersects_radius = True
            sigma_chord = math.acos(max(-1.0, min(1.0, q)))
            d_chord = EARTH_RADIUS_KM * sigma_chord
            d_entry = d_at - d_chord
            d_exit = d_at + d_chord

    # Trajectory Classification & Decision
    if is_inside:
        # Aircraft is already within the alert perimeter
        if d_at > 0.0 and cos_rel > 0.05:
            # Continuing toward closest point of approach inside radius
            time_to_cda = max(0.0, d_at / v_km_s)
            pass_cda = cda_km
            if cda_km <= max(0.5, r_km * 0.15):
                pass_type = "direct_hit"
            elif cda_km <= r_km * 0.75:
                pass_type = "near_miss"
            else:
                pass_type = "grazing"
            reason = f"Inside radius approaching CPA ({pass_cda:.2f}km) in ~{time_to_cda:.0f}s."
        elif abs(cos_rel) <= 0.05:
            # Flying tangentially to observer
            time_to_cda = None
            pass_cda = initial_dist
            pass_type = "parallel"
            reason = f"Inside radius moving tangentially at {initial_dist:.2f}km."
        else:
            # Moving away from observer (receding)
            time_to_cda = None
            pass_cda = initial_dist
            pass_type = "receding"
            reason = f"Inside radius receding from observer ({initial_dist:.2f}km)."

        return {
            "should_notify": True,
            "eta_seconds": None,
            "time_to_cda_seconds": round(time_to_cda, 1) if time_to_cda is not None else None,
            "closest_pass_km": round(pass_cda, 2),
            "pass_type": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "reason": reason,
        }

    # Aircraft is outside the alert perimeter
    if d_at <= 0.0 or cos_rel < -0.05:
        # Receding trajectory moving away from observer
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "pass_type": "receding",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "reason": f"Aircraft receding from observer ({initial_dist:.2f}km).",
        }

    if abs(cos_rel) <= 0.05:
        # Tangential trajectory
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "pass_type": "parallel",
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "reason": f"Aircraft moving tangentially outside radius at {initial_dist:.2f}km.",
        }

    # Approaching trajectory (d_at > 0, cos_rel > 0.05)
    time_to_cda = max(0.0, d_at / v_km_s)
    if cda_km <= max(0.5, r_km * 0.15):
        pass_type = "direct_hit"
    elif cda_km <= r_km * 0.75:
        pass_type = "near_miss"
    elif cda_km <= r_km:
        pass_type = "grazing"
    else:
        pass_type = "outside"

    if intersects_radius and d_entry is not None and d_entry > 0.0:
        eta_entry = max(0.0, d_entry / v_km_s)
        in_lookahead = eta_entry <= float(lookahead_seconds)
        return {
            "should_notify": in_lookahead,
            "eta_seconds": round(eta_entry, 1),
            "time_to_cda_seconds": round(time_to_cda, 1),
            "closest_pass_km": round(cda_km, 2),
            "pass_type": pass_type,
            "bearing_from_user": round(bearing_from_user, 1),
            "closure_rate_ms": round(closure_rate_ms, 1),
            "reason": (
                f"Predicted intercept in ~{eta_entry:.0f}s (CDA: {cda_km:.2f}km in ~{time_to_cda:.0f}s)."
                if in_lookahead
                else f"Entry beyond lookahead window (~{eta_entry:.0f}s > {lookahead_seconds}s)."
            ),
        }

    # Track does not enter the radius perimeter
    return {
        "should_notify": False,
        "eta_seconds": None,
        "time_to_cda_seconds": round(time_to_cda, 1),
        "closest_pass_km": round(cda_km, 2),
        "pass_type": "outside",
        "bearing_from_user": round(bearing_from_user, 1),
        "closure_rate_ms": round(closure_rate_ms, 1),
        "reason": f"Trajectory passes at {cda_km:.2f}km (outside target radius {r_km:.1f}km).",
    }


def evaluate_early_warning(
    start_lat: float,
    start_lon: float,
    speed_kts: float,
    heading_deg: float,
    turn_rate_deg_s: float,
    user_lat: float,
    user_lon: float,
    radius_km: float,
    lookahead_seconds: int = 180,
    buffer_km: float = 15.0,
) -> dict:
    """Evaluate aircraft trajectory against user target location.

    Preserves 100% backward compatibility with legacy test assertions, while
    providing rich continuous kinematics calculations.
    """
    initial_dist = haversine_distance_km(start_lat, start_lon, user_lat, user_lon)

    # 1. Fast rejection if aircraft is beyond outer buffer (user_radius + buffer_km)
    max_search_radius = radius_km + buffer_km
    speed_km_s = speed_kts * KNOTS_TO_KM_PER_SEC
    if initial_dist > max_search_radius + (speed_km_s * lookahead_seconds):
        return {
            "should_notify": False,
            "eta_seconds": None,
            "time_to_cda_seconds": None,
            "closest_pass_km": round(initial_dist, 2),
            "pass_type": "outside",
            "bearing_from_user": round(initial_bearing(user_lat, user_lon, start_lat, start_lon), 1),
            "closure_rate_ms": 0.0,
            "reason": f"Aircraft is too far ({initial_dist:.1f}km) to intercept within lookahead window.",
        }

    # Evaluate via Napier spherical geodesy & turning kinematics
    cda_result = calculate_cda_and_eta(
        aircraft_lat=start_lat,
        aircraft_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=heading_deg,
        turn_rate_deg_s=turn_rate_deg_s,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=radius_km,
        lookahead_seconds=lookahead_seconds,
    )

    # For aircraft already inside radius, maintain backward-compatible eta_seconds = 0.0
    # in evaluate_early_warning while attaching true continuous time_to_cda_seconds
    if initial_dist <= radius_km:
        return {
            "should_notify": True,
            "eta_seconds": 0.0,
            "time_to_cda_seconds": cda_result["time_to_cda_seconds"],
            "closest_pass_km": cda_result["closest_pass_km"],
            "pass_type": cda_result["pass_type"],
            "bearing_from_user": cda_result["bearing_from_user"],
            "closure_rate_ms": cda_result["closure_rate_ms"],
            "reason": cda_result["reason"],
        }

    return cda_result
