"""Multi-Tier End-to-End Kinematics & Notification Pipeline Tests.

Test Architecture:
- Tier 1: Feature Coverage (nominal flight approach, speed normalization, true distance separation, rich kinematics, lookahead window).
- Tier 2: Boundary & Corner Cases (directly overhead, boundary grazing, stationary aircraft, hypersonic speed, antimeridian wrapping).
- Tier 3: Cross-Feature Combinations (receding flight outside/inside radius, turning flight intercepting/avoiding radius, inside-circle approach).
- Tier 4: Real-World Scenarios (high-speed jet 30km approach, tangential flight corridor, departing aircraft, Trinidad & Tobago escaping, multi-aircraft monitor cycle).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import math
import os
from unittest.mock import AsyncMock, patch

# Ensure settings parse in all test environments
if not os.environ.get("ADMIN_TELEGRAM_ID"):
    os.environ["ADMIN_TELEGRAM_ID"] = "0"

from mongomock_motor import AsyncMongoMockClient
import pytest

import app.database as database
from app.aircraft.models import NormalizedAircraft
from app.config import settings
from app.worker.geo import haversine
from app.worker.kinematics import (
    EARTH_RADIUS_KM,
    KNOTS_TO_KM_PER_SEC,
    evaluate_early_warning,
    haversine_distance_km,
    initial_bearing,
    project_point,
)
from app.worker.monitor import run_monitor_cycle
from tests.test_messages import assert_valid_telegram_html, format_alert_message


# ── Test Helpers ──────────────────────────────────────────────────────────────

def _create_aircraft(
    icao24: str = "400001",
    callsign: str = "TEST100",
    lat: float = 51.70,
    lon: float = -0.12,
    velocity_ms: float = 205.77,  # ~400 kt
    heading: float = 180.0,
    aircraft_type: str = "B738",
    origin_country: str = "United Kingdom",
    altitude_m: float = 3000.0,
) -> NormalizedAircraft:
    """Helper to generate NormalizedAircraft instances."""
    return NormalizedAircraft(
        icao24=icao24,
        callsign=callsign,
        latitude=lat,
        longitude=lon,
        altitude=altitude_m,
        velocity=velocity_ms,
        heading=heading,
        vertical_rate=0.0,
        on_ground=False,
        squawk="1200",
        aircraft_type=aircraft_type,
        display_type=aircraft_type,
        origin_country=origin_country,
        last_contact=int(datetime.now(timezone.utc).timestamp()),
        source_provider="test_provider",
    )


# ── Tier 1: Feature Coverage ──────────────────────────────────────────────────

class TestTier1FeatureCoverage:
    """Tier 1: Nominal feature coverage for kinematics and alert generation."""

    def test_t1_nominal_approach_alert_generation(self) -> None:
        """Nominal flight approaching user from 25km triggers early warning alert."""
        user_lat, user_lon = 51.5074, -0.1278  # London
        radius_km = 15.0

        # Place aircraft ~25 km North heading directly South (heading 180°)
        plane_lat, plane_lon = project_point(user_lat, user_lon, 25.0, 0.0)
        speed_kts = 400.0  # ~0.205 km/s

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=speed_kts,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
            buffer_km=15.0,
        )

        assert result["should_notify"] is True
        assert result["eta_seconds"] is not None
        # Travel 10 km (from 25km to 15km) at 400 kt (~0.2057 km/s) => ~48.6s
        assert 35.0 <= result["eta_seconds"] <= 60.0
        assert result["closest_pass_km"] < 1.0  # Passes directly overhead
        assert result["pass_type"] in ("direct_hit", "near_miss")

        # Generate alert message from kinematics output
        msg = format_alert_message(
            aircraft_type="B738",
            callsign="BAW123",
            distance_km=25.0,
            eta_seconds=result["eta_seconds"],
            cda_km=result["closest_pass_km"],
        )
        assert "Early Warning Alert!" in msg
        assert "Arriving in" in msg
        assert_valid_telegram_html(msg)

    def test_t1_speed_and_heading_normalization(self) -> None:
        """Ensure velocity in m/s is properly converted to speed_kts in kinematics."""
        velocity_ms = 250.0  # ~485.96 kt
        expected_kts = velocity_ms * 1.9438444924406

        user_lat, user_lon = 40.7128, -74.0060  # New York
        radius_km = 15.0

        plane_lat, plane_lon = project_point(user_lat, user_lon, 24.0, 270.0)

        # Heading 90° directly towards user
        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=expected_kts,
            heading_deg=90.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
        )

        assert result["should_notify"] is True
        # Distance to boundary is 9 km. Speed ~0.25 km/s => ~36s
        assert 25.0 <= result["eta_seconds"] <= 50.0

    def test_t1_true_distance_vs_cda_separation(self) -> None:
        """Current aircraft distance must not be replaced by closest approach distance."""
        user_lat, user_lon = 48.8566, 2.3522  # Paris
        radius_km = 12.0

        # Aircraft currently at 22 km
        plane_lat, plane_lon = project_point(user_lat, user_lon, 22.0, 315.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=420.0,
            heading_deg=135.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )

        curr_dist = haversine_distance_km(plane_lat, plane_lon, user_lat, user_lon)
        cda_dist = result["closest_pass_km"]

        # Current distance (~22 km) is distinct from future CDA (< 1.0 km)
        assert abs(curr_dist - 22.0) < 0.5
        assert cda_dist < 1.0
        assert curr_dist > cda_dist + 15.0

        msg = format_alert_message(distance_km=curr_dist, cda_km=cda_dist)
        assert "22.0 km" in msg
        assert f"{cda_dist:.1f} km" in msg
        assert_valid_telegram_html(msg)

    def test_t1_rich_kinematics_attributes_calculation(self) -> None:
        """Evaluate initial bearing, relative closure rate, and distance geometry."""
        user_lat, user_lon = 51.5074, -0.1278
        plane_lat, plane_lon = project_point(user_lat, user_lon, 20.0, 45.0)  # NE

        bearing_from_user = initial_bearing(user_lat, user_lon, plane_lat, plane_lon)
        # Bearing from user to aircraft should be ~45° (NE)
        assert abs(bearing_from_user - 45.0) < 1.0

        # Plane flying SW (225°) directly towards user
        heading = 225.0
        speed_ms = 200.0  # ~388 kt

        # Delta angle between heading and bearing-to-user (which is 225°) is 0°
        # Closure rate should be ~200 m/s
        delta_deg = abs(heading - 225.0)
        closure_rate_ms = speed_ms * math.cos(math.radians(delta_deg))
        assert abs(closure_rate_ms - 200.0) < 0.1

        msg = format_alert_message(
            bearing_from_user=bearing_from_user,
            closure_rate_ms=closure_rate_ms,
        )
        assert "NE" in msg
        assert "45°" in msg
        assert "Closure Rate:" in msg
        assert_valid_telegram_html(msg)

    def test_t1_distant_flight_outside_lookahead_rejection(self) -> None:
        """Aircraft beyond lookahead horizon (180s) must be rejected without alert."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Plane 120 km away heading toward user at 300 kts (~0.154 km/s)
        # In 180s, plane travels ~27.7 km, reaching 92.3 km (far from 15km)
        plane_lat, plane_lon = project_point(user_lat, user_lon, 120.0, 0.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=300.0,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
            buffer_km=15.0,
        )

        assert result["should_notify"] is False
        assert result["eta_seconds"] is None
        assert result["pass_type"] in ("outside", "direct_hit", "near_miss", "grazing")


# ── Tier 2: Boundary & Corner Cases ───────────────────────────────────────────

class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary conditions, coordinate extremes, and kinematic edge cases."""

    def test_t2_plane_directly_overhead(self) -> None:
        """Plane coordinates identical to user coordinates (distance = 0 km)."""
        user_lat, user_lon = 51.5074, -0.1278

        result = evaluate_early_warning(
            start_lat=user_lat,
            start_lon=user_lon,
            speed_kts=250.0,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=15.0,
        )

        assert result["should_notify"] is True
        assert result["closest_pass_km"] == 0.0
        assert result["eta_seconds"] in (0.0, None)

        # Message builder must not divide by zero
        msg = format_alert_message(
            distance_km=0.0,
            cda_km=0.0,
            bearing_from_user=0.0,
        )
        assert "0.0 km" in msg
        assert "Direct Overhead!" in msg
        assert_valid_telegram_html(msg)

    def test_t2_plane_grazing_circle_boundary(self) -> None:
        """Tangential flight paths: 100m inside boundary alerts; 100m outside does not."""
        user_lat, user_lon = 0.0, 0.0
        radius_km = 10.0

        # Flight path parallel to Y-axis at X = 9.9 km (inside)
        # Start at (lat=0.18°, lon=9.9 km / 111.32 = 0.0889°) heading South (180°)
        inside_lon = (9.9 / EARTH_RADIUS_KM) * (180.0 / math.pi)
        result_inside = evaluate_early_warning(
            start_lat=0.15,
            start_lon=inside_lon,
            speed_kts=350.0,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            buffer_km=15.0,
        )
        assert result_inside["should_notify"] is True
        assert result_inside["closest_pass_km"] <= radius_km

        # Flight path at X = 10.2 km (outside)
        outside_lon = (10.2 / EARTH_RADIUS_KM) * (180.0 / math.pi)
        result_outside = evaluate_early_warning(
            start_lat=0.15,
            start_lon=outside_lon,
            speed_kts=350.0,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            buffer_km=15.0,
        )
        assert result_outside["should_notify"] is False
        assert result_outside["closest_pass_km"] > radius_km

    def test_t2_stationary_hovering_aircraft(self) -> None:
        """Stationary aircraft (0 kts speed) handled without division by zero."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Outside user radius (20 km away)
        out_lat, out_lon = project_point(user_lat, user_lon, 20.0, 90.0)
        res_out = evaluate_early_warning(
            start_lat=out_lat,
            start_lon=out_lon,
            speed_kts=0.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )
        assert res_out["should_notify"] is False
        assert res_out["eta_seconds"] is None

        # Inside user radius (5 km away)
        in_lat, in_lon = project_point(user_lat, user_lon, 5.0, 90.0)
        res_in = evaluate_early_warning(
            start_lat=in_lat,
            start_lon=in_lon,
            speed_kts=0.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )
        assert res_in["should_notify"] is True
        assert res_in["eta_seconds"] in (0.0, None)

    def test_t2_hypersonic_extreme_speed_aircraft(self) -> None:
        """Hypersonic aircraft at 2000 kts (~1.03 km/s) evaluated accurately."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Aircraft 55 km away heading directly at user
        plane_lat, plane_lon = project_point(user_lat, user_lon, 55.0, 270.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=2000.0,
            heading_deg=90.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
            buffer_km=50.0,
        )

        assert result["should_notify"] is True
        # Distance to boundary: 40 km. Speed: 2000 * 0.0005144 = 1.0288 km/s => ~38.8s
        assert 30.0 <= result["eta_seconds"] <= 45.0
        assert result["closest_pass_km"] < 1.0

    def test_t2_antimeridian_coordinate_wrapping(self) -> None:
        """Antimeridian crossing (-179° to +179°) does not produce 40,000 km distortion."""
        user_lat, user_lon = 0.0, -179.9
        # Plane at lon = +179.9 (approx 22.2 km away on equator across 180th meridian)
        plane_lat, plane_lon = 0.0, 179.9

        dist = haversine_distance_km(plane_lat, plane_lon, user_lat, user_lon)
        # Great-circle distance must be ~22.2 km, NOT ~40,000 km
        assert 20.0 < dist < 25.0

        # Plane flying East (90°) towards antimeridian
        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=400.0,
            heading_deg=90.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=15.0,
            buffer_km=15.0,
        )
        assert result["should_notify"] is True


# ── Tier 3: Cross-Feature Combinations ────────────────────────────────────────

class TestTier3CrossFeatureCombinations:
    """Tier 3: Complex multi-feature interactions, turns, and relative geometries."""

    def test_t3_receding_flight_outside_radius(self) -> None:
        """Aircraft in outer buffer flying away from user circle must NOT alert."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Plane 20 km North of user, flying North (heading 0° away from user)
        plane_lat, plane_lon = project_point(user_lat, user_lon, 20.0, 0.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=400.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )

        assert result["should_notify"] is False
        assert result["eta_seconds"] is None
        assert result["closest_pass_km"] >= 20.0

    def test_t3_receding_flight_inside_radius(self) -> None:
        """Aircraft inside radius but departing triggers alert with non-approaching status."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Plane 8 km North of user, flying North (0°) away from user
        plane_lat, plane_lon = project_point(user_lat, user_lon, 8.0, 0.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=250.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )

        assert result["should_notify"] is True
        # Already inside, so eta to entry is 0.0 or None
        assert result["eta_seconds"] in (0.0, None)

    def test_t3_turning_flight_intercepting_radius(self) -> None:
        """Aircraft with non-zero turn rate banking into user notification radius."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Plane 20 km North, initially heading East (90°). Turn radius ~4.1 km curves path into 15 km circle.
        plane_lat, plane_lon = project_point(user_lat, user_lon, 20.0, 0.0)

        # Right turn of 2.5°/s curves plane South towards user
        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=350.0,
            heading_deg=90.0,
            turn_rate_deg_s=2.5,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
        )

        assert result["should_notify"] is True
        assert result["pass_type"] in ("curve_intercept", "direct_hit", "pass_by")

    def test_t3_turning_flight_avoiding_radius(self) -> None:
        """Aircraft initially aimed at user but turning away avoids entering radius."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        plane_lat, plane_lon = project_point(user_lat, user_lon, 22.0, 0.0)

        # Hard left turn of -4.5°/s banks plane North, avoiding entry
        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=350.0,
            heading_deg=180.0,
            turn_rate_deg_s=-4.5,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
        )

        assert result["should_notify"] is False
        assert result["closest_pass_km"] > radius_km

    def test_t3_inside_circle_approaching_overhead(self) -> None:
        """Aircraft at 10 km inside 15 km radius, closing directly toward overhead."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Plane at 10 km South, heading North (0°) directly towards user
        plane_lat, plane_lon = project_point(user_lat, user_lon, 10.0, 180.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=300.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
        )

        assert result["should_notify"] is True
        assert result["closest_pass_km"] < 1.0


# ── Tier 4: Real-World Scenarios ──────────────────────────────────────────────

class TestTier4RealWorldScenarios:
    """Tier 4: Realistic production scenarios and complete multi-aircraft workflows."""

    def test_t4_high_speed_jet_direct_approach_30km(self) -> None:
        """Boeing 777-300ER cruising at 480 kts approaching spotter directly from 30km."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # B77W at 30 km heading 180° towards London
        plane_lat, plane_lon = project_point(user_lat, user_lon, 30.0, 0.0)
        speed_kts = 480.0

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=speed_kts,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
            buffer_km=15.0,
        )

        assert result["should_notify"] is True
        # 15 km to boundary at 480 kt (~0.247 km/s) => ~60.7s
        assert 45.0 <= result["eta_seconds"] <= 75.0
        assert result["closest_pass_km"] < 1.0

        msg = format_alert_message(
            aircraft_type="B77W",
            callsign="BAW28",
            distance_km=30.0,
            altitude_m=10972.8,  # 36,000 ft
            velocity_ms=speed_kts / 1.94384,
            heading=180.0,
            icao24="400777",
            origin_country="United Kingdom",
            eta_seconds=result["eta_seconds"],
            cda_km=result["closest_pass_km"],
        )

        assert "36,000 ft" in msg
        assert "480 kt" in msg
        assert "Arriving in" in msg
        assert "Direct Overhead" in msg
        assert_valid_telegram_html(msg)

    def test_t4_airliner_tangential_corridor_grazing(self) -> None:
        """Commercial corridor passing along the edge of user alert radius (CDA = 13.5 km)."""
        user_lat, user_lon = 51.5074, -0.1278
        radius_km = 15.0

        # Start at 22 km distance with trajectory closest approach at 13.5 km
        # Using right triangle: hypotenuse = 22 km, CDA = 13.5 km => along-track = sqrt(22^2 - 13.5^2) ≈ 17.37 km
        dist_cda = 13.5
        along_track = math.sqrt(22.0**2 - dist_cda**2)
        # User at origin, CDA point at (13.5 km East), plane starts (13.5 km East, 17.37 km North) heading South
        cda_lat, cda_lon = project_point(user_lat, user_lon, dist_cda, 90.0)
        start_lat, start_lon = project_point(cda_lat, cda_lon, along_track, 0.0)

        result = evaluate_early_warning(
            start_lat=start_lat,
            start_lon=start_lon,
            speed_kts=420.0,
            heading_deg=180.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=radius_km,
            lookahead_seconds=180,
            buffer_km=15.0,
        )

        assert result["should_notify"] is True
        assert abs(result["closest_pass_km"] - 13.5) < 0.5
        assert result["pass_type"] in ("pass_by", "grazing", "direct_hit", "near_miss")

    def test_t4_departing_aircraft_inside_radius(self) -> None:
        """C17 Globemaster climbing out at 5 km from user, departing outward."""
        user_lat, user_lon = 51.5074, -0.1278

        # 5 km away heading 0° (North) away from user
        plane_lat, plane_lon = project_point(user_lat, user_lon, 5.0, 0.0)

        result = evaluate_early_warning(
            start_lat=plane_lat,
            start_lon=plane_lon,
            speed_kts=220.0,
            heading_deg=0.0,
            turn_rate_deg_s=0.0,
            user_lat=user_lat,
            user_lon=user_lon,
            radius_km=15.0,
        )

        assert result["should_notify"] is True

        msg = format_alert_message(
            aircraft_type="C17",
            callsign="RCH440",
            distance_km=5.0,
            altitude_m=1066.8,  # 3,500 ft
            velocity_ms=113.0,
            heading=0.0,
            origin_country="United States",
            eta_seconds=None,  # Inside alert: no early warning countdown
        )

        assert "Aircraft Alert!" in msg
        assert "Early Warning Alert" not in msg
        assert "3,500 ft" in msg
        assert_valid_telegram_html(msg)

    def test_t4_special_characters_origin_country(self) -> None:
        """Alert pipeline end-to-end with Trinidad & Tobago and entity characters."""
        msg = format_alert_message(
            aircraft_type="B738 <MAX>",
            callsign="BWA&404",
            distance_km=12.5,
            altitude_m=5000.0,
            velocity_ms=210.0,
            heading=270.0,
            origin_country="Trinidad & Tobago",
            eta_seconds=120.0,
            cda_km=1.2,
        )

        assert "Trinidad &amp; Tobago" in msg
        assert "BWA&amp;404" in msg
        assert "B738 &lt;MAX&gt;" in msg
        assert "<MAX>" not in msg
        assert_valid_telegram_html(msg)

    @pytest.mark.asyncio
    async def test_t4_multi_aircraft_alert_cycle_with_mock_db(self) -> None:
        """Full worker monitor cycle with 5 concurrent aircraft in diverse kinematic states."""
        client = AsyncMongoMockClient()
        mock_db = client[settings.database_name]

        orig_client, orig_db = database._client, database._db
        database._client, database._db = client, mock_db

        try:
            user_id = 888888
            user_lat, user_lon = 51.5074, -0.1278  # London

            await database.users_col().insert_one({"user_id": user_id, "setup_complete": True})
            await database.locations_col().insert_one({
                "user_id": user_id,
                "latitude": user_lat,
                "longitude": user_lon,
                "radius_km": 15.0,
                "geohash": "gcpv",
            })
            await database.preferences_col().insert_one({
                "user_id": user_id,
                "selected_categories": ["Commercial Jets", "Military Transport"],
                "custom_aircraft": ["C17", "B738"],
            })

            # Aircraft 1: Approaching fast jet (24km North, heading South) -> SHOULD ALERT
            ac1_lat, ac1_lon = project_point(user_lat, user_lon, 24.0, 0.0)
            ac1 = _create_aircraft("400001", "APPROACH", ac1_lat, ac1_lon, velocity_ms=210.0, heading=180.0)

            # Aircraft 2: Inside circle (6km South, heading South) -> SHOULD ALERT
            ac2_lat, ac2_lon = project_point(user_lat, user_lon, 6.0, 180.0)
            ac2 = _create_aircraft("400002", "INSIDE", ac2_lat, ac2_lon, velocity_ms=180.0, heading=180.0)

            # Aircraft 3: In cooldown (already notified within 30 min) -> MUST NOT ALERT
            await database.notification_history_col().insert_one({
                "user_id": user_id,
                "aircraft_icao24": "400003",
                "cooldown_until": datetime.now(timezone.utc) + timedelta(minutes=30),
            })
            ac3_lat, ac3_lon = project_point(user_lat, user_lon, 5.0, 90.0)
            ac3 = _create_aircraft("400003", "COOLDOWN", ac3_lat, ac3_lon)

            # Aircraft 4: Receding flight outside circle (22km North, heading North away) -> MUST NOT ALERT
            ac4_lat, ac4_lon = project_point(user_lat, user_lon, 22.0, 0.0)
            ac4 = _create_aircraft("400004", "RECEDING", ac4_lat, ac4_lon, velocity_ms=210.0, heading=0.0)

            # Aircraft 5: Distant plane (75km away) -> MUST NOT ALERT
            ac5_lat, ac5_lon = project_point(user_lat, user_lon, 75.0, 45.0)
            ac5 = _create_aircraft("400005", "DISTANT", ac5_lat, ac5_lon)

            all_aircraft = [ac1, ac2, ac3, ac4, ac5]

            with (
                patch("app.worker.monitor._provider_manager.query_providers", new_callable=AsyncMock) as mock_query,
                patch("app.worker.notifications._send_message", new_callable=AsyncMock) as mock_send,
            ):
                mock_query.return_value = (all_aircraft, {"test": all_aircraft})
                mock_send.return_value = True

                await run_monitor_cycle()

                # Exactly 2 aircraft should trigger notifications (ac1 and ac2)
                assert mock_send.call_count == 2
        finally:
            database._client, database._db = orig_client, orig_db
            client.close()
