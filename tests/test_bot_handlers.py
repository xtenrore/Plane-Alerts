"""Tests for Telegram bot keyboards, messages, and state helpers."""

from app.bot.keyboards import (
    aircraft_categories_keyboard,
    category_types_sub_keyboard,
    custom_aircraft_keyboard,
    notification_feedback_keyboard,
    terms_keyboard,
)
from app.bot.messages import (
    aircraft_alert_message,
    setup_complete_message,
    status_message,
)
from app.bot.states import UserState


def test_keyboards_structure():
    """Verify inline keyboards build valid Telegram button structures."""
    kb_terms = terms_keyboard()
    assert len(kb_terms.inline_keyboard) == 1
    assert len(kb_terms.inline_keyboard[0]) == 2

    kb_cats = aircraft_categories_keyboard(
        selected_cats={"Military"},
        disabled_types=set(),
        custom_aircraft=["B738"],
    )
    assert len(kb_cats.inline_keyboard) > 0

    kb_sub = category_types_sub_keyboard("Military", disabled_types={"C17"})
    assert len(kb_sub.inline_keyboard) > 0

    kb_custom = custom_aircraft_keyboard(["B738", "A320"])
    assert len(kb_custom.inline_keyboard) > 0

    kb_feedback = notification_feedback_keyboard("notif-12345")
    assert len(kb_feedback.inline_keyboard) == 1
    assert len(kb_feedback.inline_keyboard[0]) == 2


def test_messages_formatting():
    """Verify HTML message generation."""
    msg = aircraft_alert_message(
        aircraft_type="B738",
        callsign="UAL456",
        distance_km=8.5,
        altitude_m=3000.0,
        velocity_ms=180.0,
        heading=90.0,
        icao24="a12345",
        origin_country="United States",
        eta_seconds=45.0,
    )
    assert "B738" in msg
    assert "UAL456" in msg
    assert "8.5 km" in msg
    assert "Arriving in" in msg

    status_msg = status_message(
        selected_categories=["Military"],
        custom_aircraft=["B738"],
        lat=51.5,
        lon=-0.12,
        radius_km=15.0,
        setup_complete=True,
    )
    assert "Military" in status_msg
    assert "B738" in status_msg
    assert "Monitoring active" in status_msg

    setup_msg = setup_complete_message(
        selected_categories=["Cargo"],
        custom_aircraft=[],
        lat=51.5,
        lon=-0.12,
        radius_km=20.0,
    )
    assert "Setup complete" in setup_msg


def test_user_state_constants():
    """Verify user state enumeration."""
    assert UserState.IDLE == "idle"
    assert UserState.WAITING_TERMS == "waiting_terms"
    assert UserState.WAITING_LOCATION == "waiting_location"
    assert UserState.WAITING_RADIUS == "waiting_radius"
    assert UserState.WAITING_AIRCRAFT_SELECTION == "waiting_aircraft"
    assert UserState.ADDING_CUSTOM_AIRCRAFT == "adding_custom"


def test_messages_formatting_rich_kinematics():
    """Verify rich kinematics formatting with CDA overhead badge, bearing, and closure rate."""
    msg = aircraft_alert_message(
        aircraft_type="B738",
        callsign="UAL456",
        distance_km=14.2,
        altitude_m=3000.0,
        velocity_ms=220.0,
        heading=90.0,
        icao24="a12345",
        origin_country="Trinidad & Tobago",
        eta_seconds=135.0,
        cda_km=0.8,
        trajectory_status="direct_hit",
        bearing_from_user=180.0,
        closure_rate_ms=210.0,
    )
    assert "Trinidad &amp; Tobago" in msg
    assert "Arriving in ~2m 15s" in msg
    assert "Closest Pass (CDA):" in msg
    assert "0.8 km" in msg
    assert "[Direct Overhead!]" in msg
    assert "Bearing from you:" in msg
    assert "180° (S)" in msg
    assert "Closure Rate:" in msg
    assert "closing" in msg


def test_messages_formatting_graceful_missing_fields():
    """Verify message renders cleanly without None text when fields are missing."""
    msg = aircraft_alert_message(
        aircraft_type="",
        callsign="",
        distance_km=10.0,
        altitude_m=None,
        velocity_ms=None,
        heading=None,
        icao24="",
        origin_country="",
        eta_seconds=None,
        cda_km=None,
        trajectory_status=None,
        bearing_from_user=None,
        closure_rate_ms=None,
    )
    assert "10.0 km" in msg
    assert "None km" not in msg
    assert "None ft" not in msg
    assert "None m" not in msg
    assert "Speed: None" not in msg
    assert "Aircraft Alert!" in msg
