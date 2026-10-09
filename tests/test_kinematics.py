"""Tests for kinematic physics engine and trajectory prediction.

Validates analytical O(1) Napier spherical geodesy CDA, along-track, ETA,
closure rate, receding trajectories, parallel paths, and edge cases.
"""

import math
from app.worker.kinematics import (
    calculate_cda_and_eta,
    evaluate_early_warning,
    haversine_distance_km,
    initial_bearing,
    project_point,
    simulate_trajectory,
)


def test_initial_bearing():
    """Verify cardinal bearings."""
    # North
    b_north = initial_bearing(0.0, 0.0, 1.0, 0.0)
    assert round(b_north) == 0 or round(b_north) == 360

    # East
    b_east = initial_bearing(0.0, 0.0, 0.0, 1.0)
    assert round(b_east) == 90

    # South
    b_south = initial_bearing(1.0, 0.0, 0.0, 0.0)
    assert round(b_south) == 180

    # West
    b_west = initial_bearing(0.0, 1.0, 0.0, 0.0)
    assert round(b_west) == 270


def test_project_point():
    """Projecting 111.32 km North should advance ~1 degree latitude."""
    lat, lon = project_point(0.0, 0.0, 111.32, 0.0)
    assert abs(lat - 1.0) < 0.05
    assert abs(lon - 0.0) < 0.05


def test_simulate_trajectory():
    """Trajectory simulation should generate sequential points."""
    points = simulate_trajectory(
        start_lat=50.0,
        start_lon=10.0,
        speed_kts=400.0,
        heading_deg=90.0,
        turn_rate_deg_s=0.0,
        user_lat=50.0,
        user_lon=11.0,
        lookahead_seconds=60,
        time_step_s=2,
    )
    assert len(points) == 31  # t=0, 2, 4, ..., 60
    assert points[0].seconds == 0
    assert points[-1].seconds == 60
    assert points[0].lat == 50.0


def test_evaluate_early_warning_already_inside():
    """Aircraft already inside radius should notify immediately with eta 0."""
    result = evaluate_early_warning(
        start_lat=50.0,
        start_lon=10.0,
        speed_kts=300.0,
        heading_deg=90.0,
        turn_rate_deg_s=0.0,
        user_lat=50.0,
        user_lon=10.05,
        radius_km=15.0,
    )
    assert result["should_notify"] is True
    assert result["eta_seconds"] == 0.0
    assert result["pass_type"] == "direct_hit"


def test_evaluate_early_warning_approaching():
    """Aircraft flying directly toward user should trigger early warning."""
    user_lat, user_lon = 50.0, 10.5
    # Aircraft ~21 km West, flying East (heading 90) at 450 kts (~231.5 m/s)
    # Target radius 10 km, so it enters radius in ~49s
    start_lat, start_lon = 50.0, 10.2
    result = evaluate_early_warning(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=450.0,
        heading_deg=90.0,
        turn_rate_deg_s=0.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )
    assert result["should_notify"] is True
    assert result["eta_seconds"] is not None
    assert result["eta_seconds"] >= 0
    assert result["closest_pass_km"] < 1.0
    assert result["pass_type"] == "direct_hit"
    assert result["closure_rate_ms"] > 200.0
    assert round(result["bearing_from_user"]) == 270


def test_evaluate_early_warning_flying_away():
    """Aircraft flying away should not notify."""
    user_lat, user_lon = 50.0, 10.5
    # Aircraft at 10.2 flying West (heading 270), moving away from user
    result = evaluate_early_warning(
        start_lat=50.0,
        start_lon=10.2,
        speed_kts=450.0,
        heading_deg=270.0,
        turn_rate_deg_s=0.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )
    assert result["should_notify"] is False
    assert result["pass_type"] == "receding"
    assert result["eta_seconds"] is None
    assert result["time_to_cda_seconds"] is None
    assert result["closure_rate_ms"] < 0.0


# ── Advanced Napier Spherical Geodesy Tests ──────────────────────────────────


def test_analytical_cda_head_on_approach():
    """Verify analytical O(1) CDA and ETA for direct head-on path."""
    user_lat, user_lon = 50.0, 10.5
    start_lat, start_lon = 50.0, 10.2
    speed_kts = 450.0
    v_km_s = speed_kts * 0.0005144444444444444

    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=90.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )

    initial_dist = haversine_distance_km(start_lat, start_lon, user_lat, user_lon)
    expected_cda = 0.04  # very close to 0 due to great circle curve
    expected_t_cda = initial_dist / v_km_s
    expected_entry = (initial_dist - 10.0) / v_km_s

    assert pred["should_notify"] is True
    assert abs(pred["closest_pass_km"] - expected_cda) < 0.1
    assert abs(pred["time_to_cda_seconds"] - expected_t_cda) < 1.0
    assert abs(pred["eta_seconds"] - expected_entry) < 1.0
    assert pred["pass_type"] == "direct_hit"
    assert pred["closure_rate_ms"] > 220.0
    assert 269 <= pred["bearing_from_user"] <= 271


def test_analytical_cda_lateral_offset_near_miss():
    """Aircraft passing user with lateral offset of ~5 km inside a 10 km zone."""
    # User at (50.0, 10.0). Aircraft starts South-West at (49.85, 9.8) flying due North (heading 0.0)
    # The line of flight will pass West of user at approximately CDA = distance from lon 9.8 to lon 10.0
    user_lat, user_lon = 50.0, 10.0
    start_lat, start_lon = 49.75, 9.94
    speed_kts = 400.0

    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=0.0,  # flying North
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=240,
    )

    assert pred["should_notify"] is True
    assert 3.0 < pred["closest_pass_km"] < 6.0
    assert pred["pass_type"] in ("direct_hit", "near_miss")
    assert pred["eta_seconds"] is not None
    assert pred["time_to_cda_seconds"] is not None
    assert pred["time_to_cda_seconds"] > pred["eta_seconds"]


def test_analytical_cda_grazing_trajectory():
    """Aircraft track passes outside notification radius (grazing)."""
    user_lat, user_lon = 50.0, 10.0
    # Aircraft flying North at longitude 9.80 (~14 km West), radius is 10 km
    start_lat, start_lon = 49.75, 9.80
    speed_kts = 450.0

    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=0.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )

    assert pred["should_notify"] is False
    assert pred["eta_seconds"] is None
    assert pred["closest_pass_km"] > 10.0
    assert pred["pass_type"] in ("grazing", "outside")
    assert pred["time_to_cda_seconds"] is not None  # Future closest pass point is computed


def test_analytical_cda_receding_trajectory():
    """Aircraft opening away from observer has pass_type='receding' and None ETA."""
    user_lat, user_lon = 50.0, 10.0
    start_lat, start_lon = 50.15, 10.0  # 16.7 km North
    speed_kts = 350.0

    # Flying North (heading 0) away from user
    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=0.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )

    assert pred["should_notify"] is False
    assert pred["pass_type"] == "receding"
    assert pred["eta_seconds"] is None
    assert pred["time_to_cda_seconds"] is None
    assert pred["closest_pass_km"] >= 16.0
    assert pred["closure_rate_ms"] < 0.0


def test_analytical_cda_parallel_tangential_track():
    """Aircraft moving tangentially has parallel pass_type and near-zero closure rate."""
    user_lat, user_lon = 50.0, 10.0
    # Aircraft located directly North (50.2, 10.0) flying East (heading 90)
    start_lat, start_lon = 50.2, 10.0
    speed_kts = 400.0

    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=90.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=180,
    )

    assert pred["should_notify"] is False
    assert pred["pass_type"] == "parallel"
    assert abs(pred["closure_rate_ms"]) < 10.0  # near zero radial velocity


def test_inside_radius_approaching_vs_receding():
    """Aircraft inside radius: approaching closes to CDA; receding departs."""
    user_lat, user_lon = 50.0, 10.0
    radius_km = 15.0

    # Case 1: Inside radius at 8 km, flying directly towards user
    pred_approaching = calculate_cda_and_eta(
        start_lat=49.93,
        start_lon=10.0,
        speed_kts=300.0,
        heading_deg=0.0,  # flying North towards user at 50.0
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=radius_km,
    )
    assert pred_approaching["should_notify"] is True
    assert pred_approaching["eta_seconds"] == 0.0
    assert pred_approaching["time_to_cda_seconds"] is not None
    assert pred_approaching["time_to_cda_seconds"] > 0
    assert pred_approaching["pass_type"] == "direct_hit"
    assert pred_approaching["closure_rate_ms"] > 100.0

    # Case 2: Inside radius at 8 km, flying directly away from user
    pred_receding = calculate_cda_and_eta(
        start_lat=49.93,
        start_lon=10.0,
        speed_kts=300.0,
        heading_deg=180.0,  # flying South away from user
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=radius_km,
    )
    assert pred_receding["should_notify"] is True
    assert pred_receding["eta_seconds"] == 0.0
    assert pred_receding["pass_type"] == "receding"
    assert pred_receding["time_to_cda_seconds"] is None
    assert pred_receding["exit_seconds"] is not None
    assert pred_receding["exit_seconds"] > 0
    assert pred_receding["closure_rate_ms"] < -100.0


def test_kinematics_coincident_points():
    """When aircraft is exactly at user location, no divide by zero occurs."""
    pred = calculate_cda_and_eta(
        start_lat=52.5200,
        start_lon=13.4050,
        speed_kts=250.0,
        heading_deg=90.0,
        user_lat=52.5200,
        user_lon=13.4050,
        radius_km=10.0,
    )
    assert pred["should_notify"] is True
    assert pred["eta_seconds"] == 0.0
    assert pred["closest_pass_km"] == 0.0
    assert pred["pass_type"] == "direct_hit"


def test_kinematics_stationary_aircraft():
    """Aircraft with zero speed does not crash with ZeroDivisionError."""
    # Stationary outside radius
    pred_outside = calculate_cda_and_eta(
        start_lat=52.0,
        start_lon=13.0,
        speed_kts=0.0,
        heading_deg=90.0,
        user_lat=52.5,
        user_lon=13.0,
        radius_km=10.0,
    )
    assert pred_outside["should_notify"] is False
    assert pred_outside["time_to_cda_seconds"] is None
    assert pred_outside["closure_rate_ms"] == 0.0

    # Stationary inside radius
    pred_inside = calculate_cda_and_eta(
        start_lat=52.01,
        start_lon=13.0,
        speed_kts=0.0,
        heading_deg=90.0,
        user_lat=52.0,
        user_lon=13.0,
        radius_km=10.0,
    )
    assert pred_inside["should_notify"] is True
    assert pred_inside["eta_seconds"] == 0.0


def test_kinematics_missing_heading():
    """Missing heading degrades safely without Northward assumption."""
    pred_outside = calculate_cda_and_eta(
        start_lat=52.0,
        start_lon=13.0,
        speed_kts=300.0,
        heading_deg=None,
        user_lat=52.5,
        user_lon=13.0,
        radius_km=10.0,
    )
    assert pred_outside["should_notify"] is False
    assert pred_outside["eta_seconds"] is None

    pred_inside = calculate_cda_and_eta(
        start_lat=52.01,
        start_lon=13.0,
        speed_kts=300.0,
        heading_deg=None,
        user_lat=52.0,
        user_lon=13.0,
        radius_km=10.0,
    )
    assert pred_inside["should_notify"] is True
    assert pred_inside["eta_seconds"] == 0.0


def test_kinematics_velocity_ms_compatibility():
    """Passing velocity_ms produces matching results to speed_kts."""
    v_ms = 231.5  # ~450 knots
    pred1 = calculate_cda_and_eta(
        start_lat=50.0,
        start_lon=10.2,
        velocity_ms=v_ms,
        heading_deg=90.0,
        user_lat=50.0,
        user_lon=10.5,
        radius_km=10.0,
    )
    pred2 = calculate_cda_and_eta(
        start_lat=50.0,
        start_lon=10.2,
        speed_kts=v_ms / 0.5144444444444444,
        heading_deg=90.0,
        user_lat=50.0,
        user_lon=10.5,
        radius_km=10.0,
    )
    assert abs(pred1["closest_pass_km"] - pred2["closest_pass_km"]) < 0.01
    assert abs(pred1["eta_seconds"] - pred2["eta_seconds"]) < 0.2
    assert abs(pred1["closure_rate_ms"] - pred2["closure_rate_ms"]) < 0.2


def test_kinematics_lookahead_window_cutoff():
    """Aircraft arriving beyond lookahead window does not trigger notification."""
    user_lat, user_lon = 50.0, 10.5
    # Aircraft ~50 km away, arrives in ~220s, lookahead is 120s
    start_lat, start_lon = 50.0, 9.8
    pred = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=400.0,
        heading_deg=90.0,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=10.0,
        lookahead_seconds=120,
    )
    assert pred["should_notify"] is False
    assert pred["eta_seconds"] is None
    assert pred["time_to_cda_seconds"] is not None
    assert pred["closest_pass_km"] < 1.0


def test_kinematics_poles_and_antimeridian_safety():
    """Coordinates near poles and antimeridian do not throw math domain errors."""
    # Near North pole
    pred_pole = calculate_cda_and_eta(
        start_lat=89.9,
        start_lon=0.0,
        speed_kts=300.0,
        heading_deg=180.0,
        user_lat=89.5,
        user_lon=0.0,
        radius_km=50.0,
    )
    assert isinstance(pred_pole["closest_pass_km"], float)

    # Near Antimeridian
    pred_anti = calculate_cda_and_eta(
        start_lat=0.0,
        start_lon=179.9,
        speed_kts=400.0,
        heading_deg=90.0,
        user_lat=0.0,
        user_lon=-179.9,
        radius_km=30.0,
    )
    assert isinstance(pred_anti["closest_pass_km"], float)


def test_continuous_vs_discrete_cda_consistency():
    """Analytical continuous CDA is strictly <= discrete simulated sampled minimum."""
    start_lat, start_lon = 50.0, 10.15
    user_lat, user_lon = 50.05, 10.45
    speed_kts = 420.0
    heading_deg = 80.0

    analytical = calculate_cda_and_eta(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=heading_deg,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=15.0,
        lookahead_seconds=180,
    )

    discrete_points = simulate_trajectory(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=speed_kts,
        heading_deg=heading_deg,
        turn_rate_deg_s=0.0,
        user_lat=user_lat,
        user_lon=user_lon,
        lookahead_seconds=180,
        time_step_s=2,
    )
    discrete_cda = min(p.distance_to_user_km for p in discrete_points)

    # Analytical CDA finds exact continuous minimum, so analytical <= discrete
    # and difference is within 2-second discretization interval (< 0.5 km)
    assert analytical["closest_pass_km"] <= discrete_cda + 0.05
    assert abs(analytical["closest_pass_km"] - discrete_cda) < 0.5
