"""Unit tests for Telegram bot message formatting and HTML sanitization."""

import html
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from app.bot.messages import (
    aircraft_alert_message,
    format_duration,
    format_eta_timestamp,
)


class StrictTelegramHTMLValidator(HTMLParser):
    """Adversarial HTML validator verifying that only legal Telegram Bot API tags exist."""

    ALLOWED_TAGS = {"b", "i", "u", "s", "tg-spoiler", "a", "code", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.tag_stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.ALLOWED_TAGS:
            self.errors.append(f"Forbidden HTML tag: <{tag}>")
        self.tag_stack.append(tag)
        if tag == "a":
            href_attrs = [v for k, v in attrs if k == "href"]
            if not href_attrs or not href_attrs[0]:
                self.errors.append("Anchor tag <a> missing non-empty href attribute")

    def handle_endtag(self, tag: str) -> None:
        if not self.tag_stack or self.tag_stack[-1] != tag:
            self.errors.append(f"Mismatched closing tag </{tag}> (expected {self.tag_stack[-1] if self.tag_stack else 'none'})")
        else:
            self.tag_stack.pop()

    def validate(self, html_text: str) -> None:
        self.feed(html_text)
        self.close()
        if self.tag_stack:
            self.errors.append(f"Unclosed tags remaining in stack: {self.tag_stack}")
        if self.errors:
            raise AssertionError(f"HTML validation failed: {self.errors}")


def test_format_duration_ranges():
    """Verify format_duration across all time ranges."""
    assert format_duration(None) == ""
    assert format_duration(0) == "< 10s"
    assert format_duration(5) == "< 10s"
    assert format_duration(9.9) == "< 10s"
    assert format_duration(10) == "10s"
    assert format_duration(45) == "45s"
    assert format_duration(59) == "59s"
    assert format_duration(60) == "1m"
    assert format_duration(120) == "2m"
    assert format_duration(135) == "2m 15s"
    assert format_duration(3599) == "59m 59s"
    assert format_duration(3600) == "1h"
    assert format_duration(3665) == "1h 1m"
    assert format_duration(7200) == "2h"


def test_format_eta_timestamp():
    """Verify UTC clock arrival time formatting."""
    assert format_eta_timestamp(None) == ""
    ref = datetime(2026, 10, 10, 14, 30, 0, tzinfo=timezone.utc)
    # 90 seconds later -> 14:31 UTC
    assert format_eta_timestamp(90, ref_time=ref) == "14:31 UTC"
    # 3600 seconds later -> 15:30 UTC
    assert format_eta_timestamp(3600, ref_time=ref) == "15:30 UTC"


def test_aircraft_alert_message_basic_without_eta():
    """Standard proximity alert without early warning ETA."""
    msg = aircraft_alert_message(
        aircraft_type="A320",
        callsign="DLH123",
        distance_km=7.5,
        altitude_m=2000.0,
        velocity_ms=150.0,
        heading=180.0,
        icao24="3c4d5e",
        origin_country="Germany",
    )
    assert "✈️ <b>Aircraft Alert!</b>" in msg
    assert "<b>Type:</b> <code>A320</code>" in msg
    assert "<b>Callsign:</b> <code>DLH123</code>" in msg
    assert "7.5 km away" in msg
    assert "Track on Flightradar24" in msg
    StrictTelegramHTMLValidator().validate(msg)


def test_aircraft_alert_message_early_warning_preserves_arriving_in():
    """Early warning alert must preserve 'Arriving in' phrasing for backward compatibility."""
    msg = aircraft_alert_message(
        aircraft_type="B77W",
        callsign="BAW456",
        distance_km=18.0,
        altitude_m=5000.0,
        velocity_ms=230.0,
        heading=90.0,
        icao24="400001",
        origin_country="United Kingdom",
        eta_seconds=75.0,
    )
    assert "🚀 <b>Early Warning Alert!</b>" in msg
    assert "Arriving in" in msg
    assert "Arriving in ~1m 15s" in msg
    StrictTelegramHTMLValidator().validate(msg)


def test_aircraft_alert_message_cda_direct_overhead_badge():
    """CDA < 1.0 km appends [Direct Overhead!] badge."""
    msg_overhead = aircraft_alert_message(
        aircraft_type="A359",
        callsign="AFR789",
        distance_km=12.0,
        cda_km=0.4,
        eta_seconds=60.0,
    )
    assert "Closest Pass (CDA):</b> 0.4 km" in msg_overhead
    assert "[Direct Overhead!]" in msg_overhead
    StrictTelegramHTMLValidator().validate(msg_overhead)

    msg_distant = aircraft_alert_message(
        aircraft_type="A359",
        callsign="AFR789",
        distance_km=12.0,
        cda_km=3.5,
        eta_seconds=60.0,
    )
    assert "Closest Pass (CDA):</b> 3.5 km" in msg_distant
    assert "[Direct Overhead!]" not in msg_distant
    StrictTelegramHTMLValidator().validate(msg_distant)


def test_aircraft_alert_message_bearing_and_closure_rate():
    """Bearing from user and closure rates with directional suffixes."""
    # Closing flight
    msg_closing = aircraft_alert_message(
        aircraft_type="B738",
        callsign="RYR101",
        distance_km=15.0,
        bearing_from_user=270.0,
        closure_rate_ms=210.0,
        eta_seconds=45.0,
    )
    assert "👁️ <b>Bearing:</b> W (270° from you)" in msg_closing
    assert "⚡ <b>Closure Rate:</b> 210 m/s (408 kt) closing" in msg_closing
    StrictTelegramHTMLValidator().validate(msg_closing)

    # Receding flight
    msg_receding = aircraft_alert_message(
        aircraft_type="B738",
        callsign="RYR101",
        distance_km=15.0,
        bearing_from_user=90.0,
        closure_rate_ms=-180.0,
    )
    assert "👁️ <b>Bearing:</b> E (90° from you)" in msg_receding
    assert "⚡ <b>Closure Rate:</b> 180 m/s (350 kt) receding" in msg_receding
    StrictTelegramHTMLValidator().validate(msg_receding)


def test_aircraft_alert_message_trajectory_status():
    """Trajectory status is rendered and escaped."""
    msg = aircraft_alert_message(
        aircraft_type="E190",
        callsign="KLM123",
        distance_km=10.0,
        trajectory_status="curve_intercept",
    )
    assert "🧭 <b>Trajectory:</b> curve_intercept" in msg
    StrictTelegramHTMLValidator().validate(msg)


def test_aircraft_alert_message_strict_html_escaping_adversarial():
    """Hostile XSS and HTML characters (&, <, >, quotes) must be safely escaped."""
    msg = aircraft_alert_message(
        aircraft_type="<script>alert('xss')</script>",
        callsign="A&B <C>",
        distance_km=10.0,
        icao24="abc&123",
        origin_country="Trinidad & Tobago <LLC>",
        trajectory_status="<b>injected</b>",
        eta_seconds=5.0,  # format_duration gives "< 10s", must NOT leak raw <
    )
    # Check that forbidden raw tags are NOT present
    assert "<script>" not in msg
    assert "</script>" not in msg
    assert "<b>injected</b>" not in msg
    # Check that entity equivalents are present
    assert "&lt;script&gt;" in msg
    assert "A&amp;B &lt;C&gt;" in msg
    assert "Trinidad &amp; Tobago &lt;LLC&gt;" in msg
    assert "&lt;b&gt;injected&lt;/b&gt;" in msg
    # Check that < 10s duration is properly escaped as &lt; 10s
    assert "~&lt; 10s" in msg
    # Must parse strictly as valid Telegram HTML
    StrictTelegramHTMLValidator().validate(msg)


def test_aircraft_alert_message_graceful_degradation_missing_fields():
    """All optional fields None or empty degrade gracefully without errors."""
    msg = aircraft_alert_message(
        aircraft_type="",
        callsign="",
        distance_km=5.0,
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
    assert "✈️ <b>Aircraft Alert!</b>" in msg
    assert "<code>Unknown</code>" in msg
    assert "5.0 km away" in msg
    StrictTelegramHTMLValidator().validate(msg)
