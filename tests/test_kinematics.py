"""Tests for kinematic physics engine and trajectory prediction."""

import math
import pytest

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
    # Aircraft ~20 km West, flying East (heading 90) at 450 kts (~230 m/s = ~0.8 km/s)
    # Target radius 10 km, so it will enter radius in ~15-20 seconds
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


# ── Napier Spherical Geodesy & CDA Tests ─────────────────────────────────────

def test_calculate_cda_and_eta_head_on_approach():
    """Approaching flight head-on yields near-zero CDA and direct_hit pass."""
    ulat, ulon = 50.0, 10.5
    # Aircraft 25 km West, flying East (90 deg) at 450 kts (~0.2315 km/s)
    alat, alon = 50.0, 10.15
    res = calculate_cda_and_eta(
        alat, alon, speed_kts=450.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert res["should_notify"] is True
    assert res["pass_type"] == "direct_hit"
    assert res["closest_pass_km"] < 0.1
    assert res["time_to_cda_seconds"] is not None
    assert res["eta_seconds"] is not None
    # Entry time into 10 km radius occurs before reaching user (CPA)
    assert res["time_to_cda_seconds"] > res["eta_seconds"]
    assert res["closure_rate_ms"] > 200.0
    assert 260.0 <= res["bearing_from_user"] <= 280.0


def test_calculate_cda_and_eta_lateral_offset_near_miss():
    """Aircraft offset laterally by 4 km passes at ~4 km CDA."""
    ulat, ulon = 50.0, 10.0
    # User at (50.0, 10.0), aircraft starts south-west and flies north along lon 9.95 (~3.5 km west)
    # Project start point 30 km South of (50.0, 9.95)
    start_lat, start_lon = project_point(50.0, 9.95, 30.0, 180.0)
    res = calculate_cda_and_eta(
        start_lat, start_lon, speed_kts=400.0, heading_deg=0.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert res["should_notify"] is True
    assert res["pass_type"] in ["direct_hit", "near_miss"]
    assert 3.0 <= res["closest_pass_km"] <= 4.0
    assert res["time_to_cda_seconds"] is not None
    assert res["eta_seconds"] is not None
    assert res["time_to_cda_seconds"] > res["eta_seconds"]


def test_calculate_cda_and_eta_grazing_path():
    """Aircraft passing right at radius boundary classified as grazing or outside."""
    ulat, ulon = 50.0, 10.0
    # Project path with CDA ~9.8 km (radius 10.0 km)
    # Track passes 9.8 km East of user heading North (0 deg)
    cpa_lat, cpa_lon = project_point(ulat, ulon, 9.8, 90.0)
    start_lat, start_lon = project_point(cpa_lat, cpa_lon, 30.0, 180.0)
    res = calculate_cda_and_eta(
        start_lat, start_lon, speed_kts=450.0, heading_deg=0.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert 9.6 <= res["closest_pass_km"] <= 10.0
    assert res["pass_type"] in ["grazing", "outside"]


def test_calculate_cda_and_eta_receding_flight():
    """Flight moving away from observer classified as receding with no notification."""
    ulat, ulon = 50.0, 10.0
    # Aircraft 20 km East of user, flying further East (90 deg)
    alat, alon = project_point(ulat, ulon, 20.0, 90.0)
    res = calculate_cda_and_eta(
        alat, alon, speed_kts=400.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert res["should_notify"] is False
    assert res["pass_type"] == "receding"
    assert res["eta_seconds"] is None
    assert res["time_to_cda_seconds"] is None
    assert res["closure_rate_ms"] < 0.0


def test_calculate_cda_and_eta_inside_radius_continuous():
    """Aircraft already inside radius calculates continuous time-to-CDA."""
    ulat, ulon = 50.0, 10.0
    # Aircraft 5 km West, flying East toward user (radius 15 km)
    alat, alon = project_point(ulat, ulon, 5.0, 270.0)
    res = calculate_cda_and_eta(
        alat, alon, speed_kts=300.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=15.0
    )
    assert res["should_notify"] is True
    # eta_seconds is None because already inside perimeter
    assert res["eta_seconds"] is None
    # time_to_cda_seconds is continuous and positive (~32 seconds at 300 kts)
    assert res["time_to_cda_seconds"] is not None
    assert 25.0 <= res["time_to_cda_seconds"] <= 40.0
    assert res["closest_pass_km"] < 0.1
    assert res["pass_type"] == "direct_hit"


def test_calculate_cda_and_eta_inside_radius_receding():
    """Aircraft inside radius but already past CPA and moving away."""
    ulat, ulon = 50.0, 10.0
    # Aircraft 5 km East, flying further East (90 deg)
    alat, alon = project_point(ulat, ulon, 5.0, 90.0)
    res = calculate_cda_and_eta(
        alat, alon, speed_kts=300.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=15.0
    )
    assert res["should_notify"] is True
    assert res["eta_seconds"] is None
    assert res["time_to_cda_seconds"] is None
    assert res["pass_type"] == "receding"
    assert 4.9 <= res["closest_pass_km"] <= 5.1
    assert res["closure_rate_ms"] < 0.0


def test_calculate_cda_and_eta_coincident_coordinates():
    """Aircraft directly at observer coordinates (d=0) handles zero-division safely."""
    ulat, ulon = 52.52, 13.405
    res = calculate_cda_and_eta(
        ulat, ulon, speed_kts=300.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert res["should_notify"] is True
    assert res["closest_pass_km"] == 0.0
    assert res["time_to_cda_seconds"] == 0.0
    assert res["pass_type"] == "direct_hit"
    assert res["bearing_from_user"] == 0.0
    assert res["closure_rate_ms"] == 0.0


def test_calculate_cda_and_eta_stationary_aircraft():
    """Stationary aircraft (v=0) produces None time_to_cda and 0 closure rate."""
    ulat, ulon = 52.52, 13.405
    alat, alon = project_point(ulat, ulon, 5.0, 45.0)
    res = calculate_cda_and_eta(
        alat, alon, speed_kts=0.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0
    )
    assert res["should_notify"] is True
    assert res["time_to_cda_seconds"] is None
    assert res["eta_seconds"] is None
    assert res["closure_rate_ms"] == 0.0
    assert 4.9 <= res["closest_pass_km"] <= 5.1


def test_calculate_cda_and_eta_speed_unit_interchangeability():
    """Supports speed_km_s, speed_kts, and velocity_ms equivalently."""
    ulat, ulon = 50.0, 10.5
    alat, alon = 50.0, 10.0
    # 450 kts = 231.5 m/s = 0.2315 km/s
    res_kts = calculate_cda_and_eta(alat, alon, speed_kts=450.0, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0)
    res_ms = calculate_cda_and_eta(alat, alon, velocity_ms=231.5, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0)
    res_kms = calculate_cda_and_eta(alat, alon, speed_km_s=0.2315, heading_deg=90.0, user_lat=ulat, user_lon=ulon, radius_km=10.0)

    assert res_kts["closest_pass_km"] == pytest.approx(res_ms["closest_pass_km"], abs=0.05)
    assert res_kts["closest_pass_km"] == pytest.approx(res_kms["closest_pass_km"], abs=0.05)
    assert res_kts["time_to_cda_seconds"] == pytest.approx(res_ms["time_to_cda_seconds"], abs=1.0)


def test_safe_math_domain_bounds():
    """Verify haversine and geodesy never raise ValueError on near-antipodal or polar points."""
    # Near poles
    d_pole = haversine_distance_km(89.999, 0.0, 89.999, 180.0)
    assert d_pole >= 0.0
    # Near antipodal
    d_anti = haversine_distance_km(89.999, 0.0, -89.999, 0.0)
    assert d_anti >= 0.0
    # Coincident
    assert haversine_distance_km(50.0, 10.0, 50.0, 10.0) == 0.0

    # Napier engine at extreme coordinates
    res = calculate_cda_and_eta(
        aircraft_lat=89.5,
        aircraft_lon=0.0,
        speed_kts=450.0,
        heading_deg=180.0,
        user_lat=-89.5,
        user_lon=0.0,
        radius_km=50.0,
    )
    assert isinstance(res["should_notify"], bool)
    assert isinstance(res["closest_pass_km"], float)
