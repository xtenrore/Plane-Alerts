"""Adversarial Kinematics Verification Test Suite.

Empirical verification suite authored by Kinematics Adversarial Verifier 2:
1. NormalizedAircraft edge cases: missing, None, zero, negative, extreme velocity and heading.
2. _match_user_aircraft robustness: mock users, abnormal aircraft telemetry, crash immunity.
3. Invariant check: candidate tuples strictly contain actual current distance, never future CDA.
4. Inside-radius kinematics predictions: pass_type, time_to_cda_seconds, bearing, closure rate,
   and rigorous false alarm rejection for receding/missing tracks in buffer.
"""

from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock, patch
import pytest
from mongomock_motor import AsyncMongoMockClient

import app.database as database
from app.aircraft.models import NormalizedAircraft
from app.worker.kinematics import (
    EARTH_RADIUS_KM,
    calculate_cda_and_eta,
    evaluate_early_warning,
    haversine_distance_km,
    initial_bearing,
    simulate_trajectory,
)
from app.worker.monitor import _match_user_aircraft


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def setup_mock_db():
    """Mock MongoDB database handle for monitor queries."""
    client = AsyncMongoMockClient()
    database._db = client["test_aircraft_db"]
    yield client
    database._db = None


@pytest.fixture
def base_user():
    """Standard user dictionary centered in London with 10km radius."""
    return {
        "user_id": 998877,
        "location": {
            "latitude": 51.5074,
            "longitude": -0.1278,
            "radius_km": 10.0,
        },
        "preferences": {
            "selected_categories": ["commercial", "military"],
            "disabled_types": [],
            "custom_aircraft": ["B738", "A320", "C17", "F16"],
        },
    }


# ── 1. NormalizedAircraft Edge Cases ──────────────────────────────────────────

class TestNormalizedAircraftAdversarial:
    """Stress-test NormalizedAircraft properties with extreme and abnormal values."""

    def test_missing_and_none_fields(self):
        """None velocity and heading return None for all derived properties."""
        ac = NormalizedAircraft(icao24="400001", velocity=None, heading=None)
        assert ac.speed_kts is None
        assert ac.track is None
        assert ac.ground_speed is None
        assert ac.speed is None
        assert ac.speed_kmh is None
        assert ac.has_position is False
        assert ac.display_type == "Unknown"

    def test_zero_velocity_and_heading(self):
        """Zero velocity and heading return 0.0 without divide-by-zero errors."""
        ac = NormalizedAircraft(
            icao24="400002",
            latitude=51.5,
            longitude=-0.12,
            velocity=0.0,
            heading=0.0,
            aircraft_type="B738",
        )
        assert ac.speed_kts == 0.0
        assert ac.track == 0.0
        assert ac.ground_speed == 0.0
        assert ac.speed == 0.0
        assert ac.speed_kmh == 0.0
        assert ac.has_position is True

    def test_negative_velocity_and_heading(self):
        """Negative velocity and heading values handle arithmetic safely."""
        ac = NormalizedAircraft(
            icao24="400003",
            velocity=-50.0,
            heading=-180.0,
        )
        assert ac.speed_kts == pytest.approx(-50.0 / 0.5144444444444444)
        assert ac.speed_kmh == pytest.approx(-180.0)
        assert ac.track == -180.0

    def test_extreme_and_out_of_range_velocity(self):
        """Hypersonic and superluminal velocities calculate without overflow."""
        # Hypersonic Mach ~30 (10,000 m/s)
        ac_hypersonic = NormalizedAircraft(icao24="400004", velocity=10000.0, heading=90.0)
        assert ac_hypersonic.speed_kts == pytest.approx(10000.0 / 0.5144444444444444)
        assert ac_hypersonic.speed_kmh == pytest.approx(36000.0)

        # Light speed (299,792,458 m/s)
        ac_c = NormalizedAircraft(icao24="400005", velocity=299792458.0, heading=90.0)
        assert ac_c.speed_kts > 5.8e8
        assert ac_c.speed_kmh > 1.0e9

    def test_heading_wraparound_and_multi_turns(self):
        """Headings beyond [0, 360) preserve raw heading on model."""
        ac_wrap = NormalizedAircraft(icao24="400006", heading=720.0)
        assert ac_wrap.heading == 720.0
        assert ac_wrap.track == 720.0

        ac_neg = NormalizedAircraft(icao24="400007", heading=-45.0)
        assert ac_neg.heading == -45.0
        assert ac_neg.track == -45.0


# ── 2. Candidate Distance Invariant (Actual Current Distance vs CDA) ──────────

class TestCandidateDistanceInvariant:
    """Empirically prove candidate tuples contain actual current distance, NOT CDA."""

    @pytest.mark.asyncio
    async def test_candidate_tuple_strictly_contains_current_distance(self, base_user):
        """Approaching flight 20.3km away with CDA 0.0km must pass 20.3km to notification."""
        # User is at lat=51.5074, lon=-0.1278, radius=10.0km
        # Aircraft is at lat=51.6900, lon=-0.1278 (~20.3km North)
        # Heading 180 (Due South, flying directly towards user)
        # Future CDA will be 0.00km
        ac = NormalizedAircraft(
            icao24="dist01",
            callsign="INVAR01",
            latitude=51.6900,
            longitude=-0.1278,
            velocity=200.0,  # ~388 kts
            heading=180.0,
            aircraft_type="B738",
        )

        current_dist = haversine_distance_km(ac.latitude, ac.longitude, 51.5074, -0.1278)
        assert current_dist > 15.0  # Outside radius (10km), inside buffer (25km)

        captured_notifications = []

        async def mock_send(user_id, aircraft, distance_km, notification_id="", eta_seconds=None):
            captured_notifications.append({
                "aircraft": aircraft,
                "distance_km": distance_km,
                "eta_seconds": eta_seconds,
            })
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            match_count = await _match_user_aircraft(
                base_user, [ac], {"opensky": [ac]}
            )

        assert match_count == 1
        assert len(captured_notifications) == 1
        notif = captured_notifications[0]

        # 1. Assert distance_km is the ACTUAL CURRENT DISTANCE
        assert notif["distance_km"] == pytest.approx(current_dist, abs=0.2)
        assert notif["distance_km"] > 18.0

        # 2. Assert distance_km is NOT the future CDA
        pred = getattr(ac, "_prediction", {})
        cda = pred.get("closest_pass_km", 999.0)
        assert cda < 0.5  # Future pass is < 0.5km
        assert notif["distance_km"] != pytest.approx(cda, abs=5.0)

        # 3. Assert ETA is valid positive forecast
        assert notif["eta_seconds"] is not None
        assert 30.0 < notif["eta_seconds"] < 70.0

    @pytest.mark.asyncio
    async def test_lateral_offset_approaching_aircraft_distance(self, base_user):
        """Aircraft approaching with lateral offset preserves true distance."""
        # Aircraft 22.0km away, track will pass with CDA ~ 4.5km
        # User at 51.5074, -0.1278. Aircraft at 51.68, -0.05
        ac = NormalizedAircraft(
            icao24="dist02",
            callsign="OFFSET02",
            latitude=51.68,
            longitude=-0.05,
            velocity=220.0,
            heading=190.0,
            aircraft_type="A320",
        )
        current_dist = haversine_distance_km(ac.latitude, ac.longitude, 51.5074, -0.1278)

        captured = []
        async def mock_send(user_id, aircraft, distance_km, notification_id="", eta_seconds=None):
            captured.append((distance_km, eta_seconds))
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            await _match_user_aircraft(base_user, [ac], {"dump1090": [ac]})

        assert len(captured) == 1
        distance_km, eta_seconds = captured[0]
        assert distance_km == pytest.approx(current_dist, abs=0.2)
        pred = getattr(ac, "_prediction", {})
        assert pred["closest_pass_km"] < distance_km - 5.0  # CDA is significantly closer than current dist


# ── 3. Abnormal Aircraft Telemetry & Robustness ───────────────────────────────

class TestAbnormalTelemetryRobustness:
    """Verify _match_user_aircraft survives hostile, missing, or malformed data."""

    @pytest.mark.asyncio
    async def test_missing_position_skipped_without_error(self, base_user):
        """Aircraft without latitude/longitude are ignored."""
        ac = NormalizedAircraft(icao24="bad01", latitude=None, longitude=None, aircraft_type="B738")
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_or_whitespace_type_skipped(self, base_user):
        """Aircraft with missing or blank aircraft_type are ignored."""
        ac1 = NormalizedAircraft(icao24="bad02", latitude=51.51, longitude=-0.12, aircraft_type="")
        ac2 = NormalizedAircraft(icao24="bad03", latitude=51.51, longitude=-0.12, aircraft_type="   ")
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac1, ac2], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_unwatched_aircraft_category_ignored(self, base_user):
        """Aircraft type not matching user preferences is ignored."""
        ac = NormalizedAircraft(icao24="bad04", latitude=51.51, longitude=-0.12, aircraft_type="GLID99")
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_inside_radius_missing_velocity_and_heading(self, base_user):
        """Aircraft inside radius with None velocity and None heading still triggers alert."""
        ac = NormalizedAircraft(
            icao24="inside01",
            callsign="BLIND01",
            latitude=51.51,  # ~0.3km from user
            longitude=-0.1278,
            velocity=None,
            heading=None,
            aircraft_type="B738",
        )
        captured = []
        async def mock_send(user_id, aircraft, distance_km, notification_id="", eta_seconds=None):
            captured.append((aircraft, distance_km, eta_seconds))
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            count = await _match_user_aircraft(base_user, [ac], {"opensky": [ac]})

        assert count == 1
        assert len(captured) == 1
        aircraft, dist, eta = captured[0]
        assert eta is None  # Inside radius, direct alert
        pred = getattr(aircraft, "_prediction", None)
        assert pred is not None
        assert pred["pass_type"] in ["direct_hit", "near_miss"]
        assert pred["should_notify"] is True

    @pytest.mark.asyncio
    async def test_outside_buffer_missing_velocity_or_heading_no_false_alarm(self, base_user):
        """Aircraft outside radius with None heading cannot extrapolate, NO false alarm."""
        ac = NormalizedAircraft(
            icao24="out01",
            callsign="NOHDG",
            latitude=51.65,  # ~16km from user
            longitude=-0.1278,
            velocity=200.0,
            heading=None,  # Missing heading
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_inside_radius_negative_velocity_clamped_safely(self, base_user):
        """Negative velocity inside radius does not produce negative ETA or crash."""
        ac = NormalizedAircraft(
            icao24="inside02",
            latitude=51.52,
            longitude=-0.1278,
            velocity=-100.0,
            heading=180.0,
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification", return_value=True) as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 1
            mock_send.assert_called_once()
            pred = getattr(ac, "_prediction")
            assert pred["closure_rate_ms"] == 0.0

    @pytest.mark.asyncio
    async def test_extreme_hypersonic_aircraft_in_outer_buffer(self, base_user):
        """Hypersonic craft (3000 m/s) in buffer calculates fast ETA without crash."""
        ac = NormalizedAircraft(
            icao24="hyper01",
            latitude=51.68,
            longitude=-0.1278,
            velocity=3000.0,  # ~Mach 9
            heading=180.0,
            aircraft_type="B738",
        )
        captured_eta = []
        async def mock_send(user_id, aircraft, distance_km, notification_id="", eta_seconds=None):
            captured_eta.append(eta_seconds)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            count = await _match_user_aircraft(base_user, [ac], {})

        assert count == 1
        assert len(captured_eta) == 1
        assert 0.0 < captured_eta[0] < 10.0  # Arrives in ~3-5 seconds

    @pytest.mark.asyncio
    async def test_aircraft_exactly_coincident_with_user(self, base_user):
        """Aircraft exactly at user coordinates (0m distance) does not divide by zero."""
        ac = NormalizedAircraft(
            icao24="coinc01",
            latitude=51.5074,
            longitude=-0.1278,
            velocity=150.0,
            heading=90.0,
            aircraft_type="B738",
        )
        captured = []
        async def mock_send(user_id, aircraft, distance_km, notification_id="", eta_seconds=None):
            captured.append((distance_km, getattr(aircraft, "_prediction", None)))
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            count = await _match_user_aircraft(base_user, [ac], {})

        assert count == 1
        dist, pred = captured[0]
        assert dist < 0.01  # 0 km
        assert pred["pass_type"] == "direct_hit"
        assert pred["closest_pass_km"] == 0.0
        assert pred["bearing_from_user"] == 0.0


# ── 4. Inside-Radius Kinematics Predictions & False Alarm Rejection ────────────

class TestKinematicsPredictionsAndFalseAlarmRejection:
    """Verify detailed trajectory classifications and no false alarms."""

    @pytest.mark.asyncio
    async def test_inside_radius_approaching_classification(self, base_user):
        """Aircraft inside radius heading directly at user gets direct_hit."""
        # User at 51.5074, -0.1278. Aircraft at 51.55, -0.1278 (4.7km north), heading 180 (south)
        ac = NormalizedAircraft(
            icao24="class01",
            latitude=51.55,
            longitude=-0.1278,
            velocity=200.0,
            heading=180.0,
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification", return_value=True):
            await _match_user_aircraft(base_user, [ac], {})

        pred = getattr(ac, "_prediction")
        assert pred["pass_type"] == "direct_hit"
        assert pred["time_to_cda_seconds"] is not None
        assert pred["time_to_cda_seconds"] > 0
        assert pred["closure_rate_ms"] > 150.0
        assert pred["bearing_from_user"] == pytest.approx(0.0, abs=1.0)  # Located North of user

    @pytest.mark.asyncio
    async def test_inside_radius_receding_classification(self, base_user):
        """Aircraft inside radius flying away from user gets receding and negative closure."""
        # Aircraft at 51.55, -0.1278 (4.7km north), heading 0 (north, flying away)
        ac = NormalizedAircraft(
            icao24="class02",
            latitude=51.55,
            longitude=-0.1278,
            velocity=200.0,
            heading=0.0,  # Flying away
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification", return_value=True):
            await _match_user_aircraft(base_user, [ac], {})

        pred = getattr(ac, "_prediction")
        assert pred["pass_type"] == "receding"
        assert pred["time_to_cda_seconds"] is None  # No future CDA (already past)
        assert pred["closure_rate_ms"] < -150.0  # Negative closure rate

    @pytest.mark.asyncio
    async def test_inside_radius_tangential_parallel_classification(self, base_user):
        """Aircraft inside radius on cross track gets parallel status."""
        # Aircraft at 51.55, -0.1278 (4.7km north), heading 90 (east, perpendicular)
        ac = NormalizedAircraft(
            icao24="class03",
            latitude=51.55,
            longitude=-0.1278,
            velocity=200.0,
            heading=90.0,  # East
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification", return_value=True):
            await _match_user_aircraft(base_user, [ac], {})

        pred = getattr(ac, "_prediction")
        assert pred["pass_type"] == "parallel"
        assert abs(pred["closure_rate_ms"]) < 10.0  # Near zero closure rate

    @pytest.mark.asyncio
    async def test_buffer_receding_flight_no_false_alarm(self, base_user):
        """Aircraft in outer buffer flying away (receding) MUST NOT notify."""
        # 18km North, heading North (flying away)
        ac = NormalizedAircraft(
            icao24="buf01",
            latitude=51.67,
            longitude=-0.1278,
            velocity=200.0,
            heading=0.0,  # Away
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_buffer_missing_flight_no_false_alarm(self, base_user):
        """Aircraft in outer buffer whose trajectory misses radius MUST NOT notify."""
        # 18km North, heading East (will pass at 18km CDA > 10km radius)
        ac = NormalizedAircraft(
            icao24="buf02",
            latitude=51.67,
            longitude=-0.1278,
            velocity=200.0,
            heading=90.0,  # Misses radius
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_buffer_lookahead_cutoff_no_false_alarm(self, base_user):
        """Aircraft approaching very slowly (ETA > 180s) outside lookahead window MUST NOT notify."""
        # 24km away, speed 20 m/s (~40 kts) -> takes ~700s to enter radius
        ac = NormalizedAircraft(
            icao24="buf03",
            latitude=51.72,
            longitude=-0.1278,
            velocity=20.0,
            heading=180.0,
            aircraft_type="B738",
        )
        with patch("app.worker.monitor.send_aircraft_notification") as mock_send:
            count = await _match_user_aircraft(base_user, [ac], {})
            assert count == 0
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_buffer_active_turning_avoiding_radius_no_false_alarm(self, base_user):
        """Aircraft in buffer turning away from target radius MUST NOT trigger alert."""
        # Starts 18km away heading towards user, but banking hard at 4 deg/s to the right
        ac = NormalizedAircraft(
            icao24="buf04",
            latitude=51.67,
            longitude=-0.1278,
            velocity=180.0,
            heading=180.0,
            aircraft_type="B738",
        )
        object.__setattr__(ac, "turn_rate", 4.0)  # Turns away

        # Evaluate kinematics
        pred = evaluate_early_warning(
            start_lat=ac.latitude,
            start_lon=ac.longitude,
            velocity_ms=ac.velocity,
            heading_deg=ac.heading,
            turn_rate_deg_s=4.0,
            user_lat=base_user["location"]["latitude"],
            user_lon=base_user["location"]["longitude"],
            radius_km=base_user["location"]["radius_km"],
        )
        # Because it banks 4 deg/s, after 22 seconds heading is 270 (due West)
        # and it turns completely away before reaching 10km radius
        assert pred["closest_pass_km"] > 11.0
        assert pred["should_notify"] is False
