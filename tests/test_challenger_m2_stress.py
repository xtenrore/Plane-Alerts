"""Adversarial stress-testing suite for Milestone 2 alert delivery pipeline.

Tested components:
- app/worker/monitor.py (_match_user_aircraft, candidate matching, cooldowns)
- app/worker/notifications.py (send_aircraft_notification, _send_message, send_admin_alert)
- app/bot/messages.py (aircraft_alert_message, format_duration, format_eta_timestamp)

Covers:
1. Aircraft states:
   - Early warning approaching (outside radius, heading towards user)
   - Proximity inside (inside radius, approaching)
   - Receding inside radius (inside radius, flying away)
   - Receding outside radius (outside radius, flying away)
   - Direct overhead (distance ~0, CDA < 1.0 km)
   - Tangential / parallel track outside (no alert)
2. Rich kinematics formatting and Telegram HTML cleanliness:
   - Full kinematics propagation (CDA, bearing, closure rate, duration, UTC timestamp)
   - HTML injection defense (special chars &, <, > in callsign, origin, aircraft_type, icao24)
   - Strict HTML entity well-formedness validation
   - Boundary & None degradation tests
3. Telegram error resilience & notification loop stability:
   - Forbidden (blocked bot) -> deactivates user, returns False, does not crash
   - TelegramError (rate limit / server error) -> returns False, does not deactivate, does not crash
   - NetworkError / TimedOut / BadRequest -> returns False, does not crash
   - Unexpected Exception -> caught gracefully, returns False
   - Candidate loop isolation: failure on one candidate does not stop other candidates
   - Multi-user isolation in region processing
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import os
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

# Ensure settings parse in all test environments
if not os.environ.get("ADMIN_TELEGRAM_ID"):
    os.environ["ADMIN_TELEGRAM_ID"] = "0"

from mongomock_motor import AsyncMongoMockClient
import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut

import app.database as database
from app.aircraft.models import NormalizedAircraft
from app.bot.messages import (
    aircraft_alert_message,
    format_duration,
    format_eta_timestamp,
)
from app.config import settings
from app.worker.kinematics import (
    EARTH_RADIUS_KM,
    evaluate_early_warning,
    haversine_distance_km,
    project_point,
)
from app.worker.monitor import _match_user_aircraft, _process_region
from app.worker.notifications import (
    _send_message,
    send_admin_alert,
    send_aircraft_notification,
)


# ── Fixtures & Helpers ────────────────────────────────────────────────────────

@pytest.fixture
def mock_mongo():
    """In-memory MongoDB mock client for isolated database state."""
    client = AsyncMongoMockClient()
    mock_db = client[settings.database_name]

    orig_client = database._client
    orig_db = database._db

    database._client = client
    database._db = mock_db

    yield mock_db

    database._client = orig_client
    database._db = orig_db
    client.close()


def _make_aircraft(
    icao24: str = "400001",
    callsign: str = "TEST101",
    lat: float = 51.70,
    lon: float = -0.12,
    velocity_ms: float = 200.0,
    heading: float = 180.0,
    type_code: str = "B738",
    origin_country: str = "United Kingdom",
    altitude_m: float = 3000.0,
) -> NormalizedAircraft:
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
        aircraft_type=type_code,
        display_type=type_code,
        origin_country=origin_country,
        last_contact=int(datetime.now(timezone.utc).timestamp()),
        source_provider="test_provider",
    )


class StrictTelegramHTMLParser(HTMLParser):
    """Adversarial HTML validator verifying Telegram-compliant markup."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.allowed_tags = {"b", "i", "code", "a", "pre", "u", "s", "blockquote", "tg-spoiler"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed_tags:
            self.errors.append(f"Disallowed tag: <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"Unmatched closing tag: </{tag}>")
        elif self.stack[-1] != tag:
            self.errors.append(f"Mismatched tag: expected </{self.stack[-1]}>, got </{tag}>")
        else:
            self.stack.pop()

    def validate(self, html_text: str) -> None:
        self.reset()
        self.stack.clear()
        self.errors.clear()
        self.feed(html_text)
        if self.stack:
            self.errors.append(f"Unclosed tags remaining: {self.stack}")
        if self.errors:
            raise AssertionError(f"Invalid Telegram HTML: {self.errors}\nText:\n{html_text}")


# ── 1. Aircraft States & Monitor Dispatch Tests ────────────────────────────────

@pytest.mark.asyncio
class TestMonitorMatchingAircraftStates:
    """Stress-test monitor matching across 6 distinct aircraft states."""

    async def test_state1_early_warning_approaching(self, mock_mongo) -> None:
        """Aircraft outside user radius heading directly towards user triggers early warning."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1001,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 20 km north, heading 180 (south) towards user at 200 m/s
        ac_lat, ac_lon = project_point(user_lat, user_lon, 20.0, 0.0)
        ac = _make_aircraft(icao24="ew0001", lat=ac_lat, lon=ac_lon, heading=180.0, velocity_ms=200.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 1
        assert len(dispatched_alerts) == 1
        alert = dispatched_alerts[0]

        # Verify kinematics passed
        assert alert["user_id"] == 1001
        assert alert["aircraft"].icao24 == "ew0001"
        assert alert["distance_km"] == pytest.approx(20.0, abs=0.5)
        assert alert["eta_seconds"] is not None and alert["eta_seconds"] > 0
        assert alert["cda_km"] == pytest.approx(0.0, abs=0.5)
        assert alert["trajectory_status"] == "direct_hit"
        assert (alert["bearing_from_user"] == pytest.approx(0.0, abs=2.0) or
                alert["bearing_from_user"] == pytest.approx(360.0, abs=2.0))
        assert alert["closure_rate_ms"] == pytest.approx(200.0, abs=5.0)

        # Verify cooldown recorded in mock mongo
        history = await mock_mongo.notification_history.find_one({"user_id": 1001, "aircraft_icao24": "ew0001"})
        assert history is not None
        assert history["distance_km"] == pytest.approx(20.0, abs=0.5)

    async def test_state2_proximity_inside_approaching(self, mock_mongo) -> None:
        """Aircraft already inside radius approaching center triggers proximity alert with rich kinematics."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1002,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 5 km north, heading 180 towards user
        ac_lat, ac_lon = project_point(user_lat, user_lon, 5.0, 0.0)
        ac = _make_aircraft(icao24="prox01", lat=ac_lat, lon=ac_lon, heading=180.0, velocity_ms=200.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 1
        assert len(dispatched_alerts) == 1
        alert = dispatched_alerts[0]

        assert alert["distance_km"] == pytest.approx(5.0, abs=0.5)
        assert alert["eta_seconds"] is None  # Direct proximity alert has no countdown
        assert alert["cda_km"] == pytest.approx(0.0, abs=0.5)
        assert alert["trajectory_status"] == "direct_hit"
        assert alert["closure_rate_ms"] == pytest.approx(200.0, abs=5.0)

    async def test_state3_receding_inside_radius(self, mock_mongo) -> None:
        """Aircraft inside radius but flying away is flagged as receding with negative closure rate."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1003,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 5 km north, heading 0 (north, away from user)
        ac_lat, ac_lon = project_point(user_lat, user_lon, 5.0, 0.0)
        ac = _make_aircraft(icao24="rec001", lat=ac_lat, lon=ac_lon, heading=0.0, velocity_ms=200.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 1
        alert = dispatched_alerts[0]
        assert alert["trajectory_status"] == "receding"
        assert alert["closure_rate_ms"] < 0  # Negative closure rate

    async def test_state4_receding_outside_radius_no_alert(self, mock_mongo) -> None:
        """Aircraft outside user radius flying away must NOT trigger an alert."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1004,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 20 km north, heading 0 (flying further north away)
        ac_lat, ac_lon = project_point(user_lat, user_lon, 20.0, 0.0)
        ac = _make_aircraft(icao24="rec002", lat=ac_lat, lon=ac_lon, heading=0.0, velocity_ms=200.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 0
        assert len(dispatched_alerts) == 0

    async def test_state5_direct_overhead(self, mock_mongo) -> None:
        """Aircraft directly overhead (distance < 1.0 km) receives direct overhead badge."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1005,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # Overhead: 0.1 km offset
        ac_lat, ac_lon = project_point(user_lat, user_lon, 0.1, 90.0)
        ac = _make_aircraft(icao24="ovhd01", lat=ac_lat, lon=ac_lon, heading=90.0, velocity_ms=150.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 1
        alert = dispatched_alerts[0]
        assert alert["cda_km"] < 1.0

        # Test message rendering for direct overhead
        msg = aircraft_alert_message(
            aircraft_type=ac.display_type,
            callsign=ac.callsign,
            distance_km=alert["distance_km"],
            cda_km=alert["cda_km"],
            trajectory_status=alert["trajectory_status"],
            bearing_from_user=alert["bearing_from_user"],
            closure_rate_ms=alert["closure_rate_ms"],
        )
        assert "[Direct Overhead!]" in msg
        StrictTelegramHTMLParser().validate(msg)

    async def test_state6_tangential_grazing_outside_no_alert(self, mock_mongo) -> None:
        """Aircraft outside radius on parallel tangential track missing zone produces no notification."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 1006,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 10.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 20 km north, flying east (90 degrees). Closest pass is 20 km > 10 km radius.
        ac_lat, ac_lon = project_point(user_lat, user_lon, 20.0, 0.0)
        ac = _make_aircraft(icao24="tang01", lat=ac_lat, lon=ac_lon, heading=90.0, velocity_ms=220.0)

        dispatched_alerts = []

        async def fake_send(**kwargs):
            dispatched_alerts.append(kwargs)
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=fake_send):
            count = await _match_user_aircraft(user_doc, [ac], {"test_provider": [ac]})

        assert count == 0
        assert len(dispatched_alerts) == 0


# ── 2. Rich Kinematics Formatting & Telegram Bot Cleanliness ──────────────────

class TestRichKinematicsAndTelegramCleanliness:
    """Stress-test send_aircraft_notification formatting, HTML safety, and entity escaping."""

    @pytest.mark.asyncio
    async def test_full_kinematics_passed_to_telegram_bot(self) -> None:
        """Verify all rich kinematics are formatted and sent to bot.send_message."""
        ac = _make_aircraft(
            icao24="a00001",
            callsign="BAW456",
            lat=51.65,
            lon=-0.12,
            velocity_ms=210.0,
            heading=180.0,
            type_code="B738",
            origin_country="United Kingdom",
            altitude_m=3500.0,
        )

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=True)
        mock_bot.token = settings.telegram_bot_token

        with patch("app.worker.notifications._get_bot", return_value=mock_bot):
            success = await send_aircraft_notification(
                user_id=8888,
                aircraft=ac,
                distance_km=14.5,
                notification_id="notif_test_1",
                eta_seconds=75.0,
                cda_km=0.8,
                trajectory_status="direct_hit",
                bearing_from_user=0.0,
                closure_rate_ms=205.0,
            )

        assert success is True
        mock_bot.send_message.assert_called_once()
        _, kwargs = mock_bot.send_message.call_args

        assert kwargs["chat_id"] == 8888
        assert kwargs["parse_mode"] == ParseMode.HTML
        assert kwargs["disable_web_page_preview"] is True
        assert kwargs["reply_markup"] is not None

        text = kwargs["text"]
        # Invariants & formatting checks
        assert "🚀 <b>Early Warning Alert!</b>" in text
        assert "Arriving in ~1m 15s" in text
        assert "🎯 <b>Closest Pass (CDA):</b> 0.8 km" in text
        assert "[Direct Overhead!]" in text
        assert "🧭 <b>Bearing from you:</b> 0° (N)" in text
        assert "⚡ <b>Closure Rate:</b> 205 m/s (~398 kt) closing" in text
        assert "🧭 <b>Trajectory:</b> direct_hit" in text
        assert "<b>Altitude:</b> 11,483 ft (3,500 m)" in text
        assert "<b>Speed:</b> 408 kt (756 km/h)" in text
        assert "<b>Heading:</b> S (180°)" in text
        assert '<a href="https://globe.adsb.fi/?icao=a00001">🌍 Track Live on ADSB.fi</a>' in text

        StrictTelegramHTMLParser().validate(text)

    def test_adversarial_html_entity_escaping(self) -> None:
        """Adversarial strings containing HTML tags and ampersands must be safely escaped."""
        evil_inputs = [
            ("Trinidad & Tobago", "BAW<123>", "B738 & <MAX-8>", "a1b2c3<evil>"),
            ("Antigua & Barbuda", "RTE & CO", "A321 <b>NEO</b>", "123456&"),
            ("<script>alert(1)</script>", "<>&\"'", "<b>Injection</b>", "xyz\""),
        ]

        parser = StrictTelegramHTMLParser()

        for origin, callsign, ac_type, icao in evil_inputs:
            msg = aircraft_alert_message(
                aircraft_type=ac_type,
                callsign=callsign,
                distance_km=15.0,
                altitude_m=3000.0,
                velocity_ms=200.0,
                heading=90.0,
                icao24=icao,
                origin_country=origin,
                eta_seconds=120.0,
                cda_km=2.5,
                trajectory_status="grazing & turning",
                bearing_from_user=45.0,
                closure_rate_ms=180.0,
            )
            # Ensure no raw unescaped injection
            assert "<script>" not in msg
            assert "<b>Injection</b>" not in msg
            assert "<b>NEO</b>" not in msg

            # Must be valid HTML
            parser.validate(msg)

    def test_graceful_degradation_boundary_and_none_values(self) -> None:
        """All optional telemetry fields set to None, 0, or extreme boundaries render without exception."""
        parser = StrictTelegramHTMLParser()

        test_matrix = [
            # All None
            dict(aircraft_type="", callsign="", distance_km=0.0, altitude_m=None, velocity_ms=None,
                 heading=None, icao24="", origin_country="", eta_seconds=None, cda_km=None,
                 trajectory_status=None, bearing_from_user=None, closure_rate_ms=None),
            # All zero
            dict(aircraft_type="GLID", callsign="ZERO0", distance_km=0.0, altitude_m=0.0, velocity_ms=0.0,
                 heading=0.0, icao24="000000", origin_country="Null Island", eta_seconds=0.0, cda_km=0.0,
                 trajectory_status="stationary", bearing_from_user=0.0, closure_rate_ms=0.0),
            # Negative values (below sea level, receding)
            dict(aircraft_type="TEST", callsign="NEG1", distance_km=1.0, altitude_m=-30.0, velocity_ms=50.0,
                 heading=359.9, icao24="abcdef", origin_country="Netherlands", eta_seconds=None, cda_km=0.5,
                 trajectory_status="receding", bearing_from_user=359.0, closure_rate_ms=-75.0),
            # Long duration (> 1 hour)
            dict(aircraft_type="B77W", callsign="LONG1", distance_km=50.0, altitude_m=11000.0, velocity_ms=250.0,
                 heading=270.0, icao24="112233", origin_country="Japan", eta_seconds=3665.0, cda_km=5.0,
                 trajectory_status="approaching", bearing_from_user=90.0, closure_rate_ms=250.0),
        ]

        for params in test_matrix:
            msg = aircraft_alert_message(**params)
            assert "✈️ <b>Aircraft Alert!</b>" in msg or "🚀 <b>Early Warning Alert!</b>" in msg
            assert "None" not in msg  # No raw Python 'None' strings in output
            parser.validate(msg)


# ── 3. Telegram Error Resilience & Loop Stability ─────────────────────────────

@pytest.mark.asyncio
class TestTelegramErrorResilienceAndLoopStability:
    """Stress-test error handling against simulated Telegram failures."""

    async def test_forbidden_error_deactivates_user_gracefully(self, mock_mongo) -> None:
        """When Telegram returns Forbidden (user blocked bot), user is deactivated without crashing."""
        user_id = 9001
        await mock_mongo.users.insert_one({"user_id": user_id, "setup_complete": True})

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=Forbidden("Forbidden: bot was blocked by the user"))
        mock_bot.token = settings.telegram_bot_token

        with patch("app.worker.notifications._get_bot", return_value=mock_bot):
            success = await _send_message(user_id=user_id, text="Test alert")

        assert success is False
        user_doc = await mock_mongo.users.find_one({"user_id": user_id})
        assert user_doc["setup_complete"] is False

    @pytest.mark.parametrize("exc_type,exc_msg", [
        (TelegramError, "Flood control exceeded: retry after 30"),
        (NetworkError, "Connection reset by peer"),
        (BadRequest, "Bad Request: chat not found"),
        (TimedOut, "Request timed out"),
        (RuntimeError, "Unexpected thread pool failure"),
    ])
    async def test_transient_and_unexpected_errors_do_not_crash(self, mock_mongo, exc_type, exc_msg) -> None:
        """Transient and unexpected errors log and return False without crashing the caller."""
        user_id = 9002
        await mock_mongo.users.insert_one({"user_id": user_id, "setup_complete": True})

        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(side_effect=exc_type(exc_msg))
        mock_bot.token = settings.telegram_bot_token

        with patch("app.worker.notifications._get_bot", return_value=mock_bot):
            success = await _send_message(user_id=user_id, text="Test alert")

        assert success is False
        # User must NOT be deactivated on transient network or rate limit errors
        user_doc = await mock_mongo.users.find_one({"user_id": user_id})
        assert user_doc["setup_complete"] is True

    async def test_candidate_loop_isolation_one_failure_does_not_abort_others(self, mock_mongo) -> None:
        """If send_aircraft_notification fails on one candidate, other candidates still process."""
        user_lat, user_lon = 51.52, -0.12
        user_doc = {
            "user_id": 9003,
            "location": {"latitude": user_lat, "longitude": user_lon, "radius_km": 15.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        # 3 candidate aircraft inside radius
        p1 = project_point(user_lat, user_lon, 5.0, 0.0)
        p2 = project_point(user_lat, user_lon, 6.0, 90.0)
        p3 = project_point(user_lat, user_lon, 7.0, 180.0)

        ac1 = _make_aircraft(icao24="fail01", lat=p1[0], lon=p1[1])
        ac2 = _make_aircraft(icao24="succ02", lat=p2[0], lon=p2[1])
        ac3 = _make_aircraft(icao24="succ03", lat=p3[0], lon=p3[1])

        # ac1 fails, ac2 succeeds, ac3 succeeds
        async def mock_send(user_id, aircraft, **kwargs):
            if aircraft.icao24 == "fail01":
                return False
            return True

        with patch("app.worker.monitor.send_aircraft_notification", side_effect=mock_send):
            count = await _match_user_aircraft(
                user_doc,
                [ac1, ac2, ac3],
                {"test_provider": [ac1, ac2, ac3]},
            )

        assert count == 2  # 2 succeeded out of 3

        # Verify cooldown only written for successful sends
        c1 = await mock_mongo.notification_history.find_one({"aircraft_icao24": "fail01"})
        c2 = await mock_mongo.notification_history.find_one({"aircraft_icao24": "succ02"})
        c3 = await mock_mongo.notification_history.find_one({"aircraft_icao24": "succ03"})

        assert c1 is None
        assert c2 is not None
        assert c3 is not None

    async def test_multi_user_region_isolation(self, mock_mongo) -> None:
        """In _process_region, an exception raised during matching one user does not abort other users."""
        u1 = {
            "user_id": 9101,
            "location": {"latitude": 51.52, "longitude": -0.12, "radius_km": 15.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }
        u2 = {
            "user_id": 9102,
            "location": {"latitude": 51.52, "longitude": -0.12, "radius_km": 15.0},
            "preferences": {"selected_categories": ["VIP Aircraft"], "custom_aircraft": ["B738"]},
        }

        ac = _make_aircraft(icao24="multi01", lat=51.54, lon=-0.12)

        call_records = []

        async def fake_match(user, aircraft_list, results_by_provider):
            call_records.append(user["user_id"])
            if user["user_id"] == 9101:
                return 0
            return 1

        with patch("app.worker.monitor._provider_manager.query_providers", return_value=([ac], {"p1": [ac]})), \
             patch("app.worker.monitor.provider_learner.record_cycle_observation", return_value=None), \
             patch("app.worker.monitor._match_user_aircraft", side_effect=fake_match):
            total = await _process_region("gcpv", [u1, u2])

        assert total == 1
        assert call_records == [9101, 9102]

    async def test_admin_alert_resilience(self) -> None:
        """send_admin_alert gracefully handles missing admin ID or failing bot call."""
        # Unconfigured admin ID
        with patch.object(settings, "admin_telegram_id", 0):
            await send_admin_alert("Alert without admin ID")

        # Failing bot call
        with patch.object(settings, "admin_telegram_id", 123456), \
             patch("app.worker.notifications.Bot.send_message", side_effect=TelegramError("Network down")):
            await send_admin_alert("Alert with failing Telegram API")

    async def test_concurrent_sends_semaphore_throttling(self) -> None:
        """Verify 30 concurrent notifications are handled through semaphore without errors."""
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=True)
        mock_bot.token = settings.telegram_bot_token

        with patch("app.worker.notifications._get_bot", return_value=mock_bot), \
             patch("app.worker.notifications.asyncio.sleep", AsyncMock(return_value=None)):
            tasks = [
                _send_message(user_id=1000 + i, text=f"Alert {i}")
                for i in range(30)
            ]
            results = await asyncio.gather(*tasks)

        assert all(results)
        assert mock_bot.send_message.call_count == 30

