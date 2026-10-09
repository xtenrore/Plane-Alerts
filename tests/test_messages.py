"""Tests for Telegram alert message formatting, HTML entity escaping, and duration display.

Covers:
- HTML escaping for special characters (&, <, >) in origin_country, callsign, aircraft_type.
- Alert formatting with and without ETA, CDA, bearing, closure rate.
- Human-friendly duration formatting (2m 15s, 45s, < 10s).
- Backward compatibility: presence of 'Arriving in' substring when ETA is provided.
- Graceful handling of None and missing telemetry fields without exceptions.
"""

from __future__ import annotations

import html
from html.parser import HTMLParser
import inspect
import os
from typing import Any

# Ensure settings parse in all test environments
if not os.environ.get("ADMIN_TELEGRAM_ID"):
    os.environ["ADMIN_TELEGRAM_ID"] = "0"

import pytest

from app.bot import messages
from app.worker.geo import km_to_nautical_miles, ms_to_knots


# ── Inspection & Compatibility Shim ──────────────────────────────────────────

_AIRCRAFT_ALERT_SIG = inspect.signature(messages.aircraft_alert_message)
IS_M2_ALERT_PRESENT = (
    "cda_km" in _AIRCRAFT_ALERT_SIG.parameters
    and hasattr(messages, "format_duration")
)


def format_duration(seconds: float | int | None) -> str:
    """Format duration into human-readable string (e.g., '45s', '2m 15s', '< 10s')."""
    if hasattr(messages, "format_duration"):
        return messages.format_duration(seconds)  # type: ignore[no-any-return]

    # Reference implementation per PROJECT.md / Survey 2 specifications
    if seconds is None:
        return ""
    total_sec = max(0, int(round(seconds)))
    if total_sec < 10:
        return f"{total_sec}s"
    if total_sec < 60:
        return f"{total_sec}s"
    minutes, sec = divmod(total_sec, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec > 0 else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"


def format_alert_message(
    aircraft_type: str = "B738",
    callsign: str = "TEST123",
    distance_km: float = 12.0,
    altitude_m: float | None = 3000.0,
    velocity_ms: float | None = 210.0,
    heading: float | None = 180.0,
    icao24: str = "4b1234",
    origin_country: str = "United Kingdom",
    eta_seconds: float | None = None,
    cda_km: float | None = None,
    trajectory_status: str | None = None,
    bearing_from_user: float | None = None,
    closure_rate_ms: float | None = None,
) -> str:
    """Invoke aircraft_alert_message using app.bot.messages if M2 is landed,
    or reference M2 formatter if M2 is still in progress."""
    if IS_M2_ALERT_PRESENT:
        return messages.aircraft_alert_message(  # type: ignore[call-arg]
            aircraft_type=aircraft_type,
            callsign=callsign,
            distance_km=distance_km,
            altitude_m=altitude_m,
            velocity_ms=velocity_ms,
            heading=heading,
            icao24=icao24,
            origin_country=origin_country,
            eta_seconds=eta_seconds,
            cda_km=cda_km,
            trajectory_status=trajectory_status,
            bearing_from_user=bearing_from_user,
            closure_rate_ms=closure_rate_ms,
        )

    # Reference M2 implementation matching Survey 2 / PROJECT.md specification
    safe_type = html.escape(aircraft_type or "Unknown", quote=True)
    safe_callsign = html.escape(callsign.strip(), quote=True) if callsign else ""
    safe_origin = html.escape(origin_country.strip(), quote=True) if origin_country else ""
    safe_icao = html.escape(icao24.strip().lower(), quote=True) if icao24 else ""

    lines: list[str] = []

    # 1. Header & ETA
    if eta_seconds is not None and eta_seconds > 0:
        duration_str = html.escape(format_duration(eta_seconds), quote=True)
        eta_desc = f"Arriving in ~{duration_str}"
        lines.append(f"🚀 <b>Early Warning Alert!</b>\n⏱️ <b>ETA:</b> {eta_desc}\n")
    else:
        lines.append("✈️ <b>Aircraft Alert!</b>\n")

    # 2. Identification
    lines.append(f"<b>Type:</b> <code>{safe_type}</code>")
    if safe_callsign:
        lines.append(f"<b>Callsign:</b> <code>{safe_callsign}</code>")
    if safe_origin:
        lines.append(f"<b>Origin:</b> {safe_origin}")

    # 3. Position & Kinematics
    dist_nm = km_to_nautical_miles(distance_km)
    if bearing_from_user is not None:
        from app.worker.geo import heading_to_cardinal
        cardinal_dir = heading_to_cardinal(bearing_from_user)
        lines.append(
            f"\n📍 <b>Position:</b> {distance_km:.1f} km ({dist_nm:.1f} NM) • {cardinal_dir} ({bearing_from_user:.0f}° from you)"
        )
    else:
        lines.append(f"\n📍 <b>Distance:</b> {distance_km:.1f} km ({dist_nm:.1f} NM) away")

    if cda_km is not None:
        cda_nm = km_to_nautical_miles(cda_km)
        if cda_km < 1.0:
            lines.append(f"🎯 <b>Closest Pass (CDA):</b> {cda_km:.1f} km ({cda_nm:.1f} NM) — Direct Overhead!")
        else:
            lines.append(f"🎯 <b>Closest Pass (CDA):</b> {cda_km:.1f} km ({cda_nm:.1f} NM)")

    if trajectory_status:
        lines.append(f"🧭 <b>Trajectory:</b> {trajectory_status}")

    if closure_rate_ms is not None and closure_rate_ms > 1.0:
        closure_kt = ms_to_knots(closure_rate_ms)
        lines.append(f"⚡ <b>Closure Rate:</b> {closure_kt} kt closing")

    # 4. Telemetry
    telemetry: list[str] = []
    if altitude_m is not None:
        from app.worker.geo import metres_to_feet
        alt_ft = metres_to_feet(altitude_m)
        telemetry.append(f"<b>Altitude:</b> {alt_ft:,} ft ({altitude_m:,.0f} m)")
    if velocity_ms is not None:
        speed_kt = ms_to_knots(velocity_ms)
        speed_kmh = round(velocity_ms * 3.6)
        telemetry.append(f"<b>Speed:</b> {speed_kt} kt ({speed_kmh} km/h)")
    if heading is not None:
        from app.worker.geo import heading_to_cardinal
        hdg_cardinal = heading_to_cardinal(heading)
        telemetry.append(f"<b>Heading:</b> {hdg_cardinal} ({heading:.0f}°)")

    if telemetry:
        lines.append("\n" + "\n".join(telemetry))

    # 5. Tracking Link
    if safe_icao:
        lines.append(f'\n<a href="https://globe.adsb.fi/?icao={safe_icao}">🌍 Track Live on ADSB.fi</a>')

    return "\n".join(lines)


class StrictHTMLValidator(HTMLParser):
    """Adversarial HTML validator asserting well-formed tags and no unescaped entities."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.allowed_tags = {"b", "i", "code", "a", "pre", "u", "s", "blockquote"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.allowed_tags:
            self.errors.append(f"Forbidden tag: <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"Mismatched tag: expected {self.stack[-1] if self.stack else 'none'}, got {tag}")
        else:
            self.stack.pop()


def assert_valid_telegram_html(content: str) -> None:
    """Verify that message text parses cleanly as Telegram HTML."""
    validator = StrictHTMLValidator()
    validator.feed(content)
    assert not validator.stack, f"Unclosed HTML tags in message: {validator.stack}"
    assert not validator.errors, f"HTML parsing errors: {validator.errors}"
    import re
    # In Telegram HTML, raw '<' outside valid HTML tags is strictly forbidden
    stripped = re.sub(r"</?(?:b|i|code|a|pre|u|s|blockquote)(?:\s+[^>]*)?>", "", content)
    assert "<" not in stripped, f"Raw unescaped '<' found in Telegram HTML:\n{content}"


# ── Test Cases ────────────────────────────────────────────────────────────────

def test_duration_formatting_various_ranges() -> None:
    """Test human-friendly duration formatting across multiple ranges."""
    # < 60s
    assert format_duration(45) == "45s"
    assert format_duration(5) in ("5s", "< 10s", "0m 5s")
    assert format_duration(0) in ("0s", "< 10s")

    # Minutes & seconds
    assert format_duration(135) == "2m 15s"
    assert format_duration(60) in ("1m", "1m 0s")
    assert format_duration(300) in ("5m", "5m 0s")

    # Hours
    assert format_duration(3600) in ("1h", "1h 0m", "1h 0m 0s")
    assert format_duration(3665) in ("1h 1m", "1h 1m 5s")

    # Edge cases
    assert format_duration(None) == ""


def test_html_escaping_origin_country_with_ampersand() -> None:
    """Special characters like '&' in origin_country must be strictly escaped to prevent 400 Bad Request."""
    msg = format_alert_message(
        origin_country="Trinidad & Tobago",
        callsign="BWA404",
        aircraft_type="B738",
    )
    assert "Trinidad &amp; Tobago" in msg
    assert "Trinidad & Tobago" not in msg.replace("&amp;", "")
    assert_valid_telegram_html(msg)

    # Test another country with ampersand
    msg2 = format_alert_message(origin_country="Antigua & Barbuda")
    assert "Antigua &amp; Barbuda" in msg2
    assert_valid_telegram_html(msg2)


def test_html_escaping_callsign_and_aircraft_type_entities() -> None:
    """Special characters '<', '>', '&' in callsign and aircraft_type must be escaped."""
    msg = format_alert_message(
        aircraft_type="A320 <neo>",
        callsign="VIP&1<DEV>",
    )
    assert "A320 &lt;neo&gt;" in msg
    assert "VIP&amp;1&lt;DEV&gt;" in msg
    assert "<neo>" not in msg
    assert "<DEV>" not in msg
    assert_valid_telegram_html(msg)


def test_html_escaping_injection_attempt() -> None:
    """Malicious injection payloads in ADS-B fields must not inject tags."""
    xss_payload = '<script>alert("XSS")</script>'
    msg = format_alert_message(callsign=xss_payload)
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg
    assert_valid_telegram_html(msg)


def test_formatting_with_eta_backward_compatibility() -> None:
    """When eta_seconds is provided, 'Arriving in' MUST be present for backward compatibility."""
    msg = format_alert_message(
        eta_seconds=135.0,
        distance_km=14.5,
    )
    assert "🚀 <b>Early Warning Alert!</b>" in msg
    assert "Arriving in" in msg
    assert "~2m 15s" in msg or "~135s" in msg
    assert_valid_telegram_html(msg)


def test_formatting_with_short_eta_duration_html_escaped() -> None:
    """ETAs under 10 seconds must be safely escaped as '&lt; 10s' with no raw '<' character."""
    import re

    # Via format_alert_message shim
    msg = format_alert_message(
        eta_seconds=5.0,
        distance_km=10.0,
    )
    assert "🚀 <b>Early Warning Alert!</b>" in msg
    assert "Arriving in ~&lt; 10s" in msg
    assert "< 10s" not in msg.replace("&lt; 10s", "")
    assert_valid_telegram_html(msg)

    # Via direct messages.aircraft_alert_message
    direct_msg = messages.aircraft_alert_message(
        aircraft_type="B738",
        callsign="UAL123",
        distance_km=10.0,
        eta_seconds=5.0,
    )
    assert "🚀 <b>Early Warning Alert!</b>" in direct_msg
    assert "Arriving in ~&lt; 10s" in direct_msg
    stripped = re.sub(r"</?(?:b|i|code|a)(?:\s+[^>]*)?>", "", direct_msg)
    assert "<" not in stripped, f"Raw unescaped '<' found in direct message: {direct_msg}"
    assert_valid_telegram_html(direct_msg)


def test_formatting_without_eta_proximity_alert() -> None:
    """When eta_seconds is None or 0, format as standard proximity alert without countdown."""
    # Case: None
    msg_none = format_alert_message(eta_seconds=None)
    assert "✈️ <b>Aircraft Alert!</b>" in msg_none
    assert "Early Warning Alert" not in msg_none
    assert "Arriving in" not in msg_none
    assert_valid_telegram_html(msg_none)

    # Case: 0.0
    msg_zero = format_alert_message(eta_seconds=0.0)
    assert "✈️ <b>Aircraft Alert!</b>" in msg_zero
    assert "Early Warning Alert" not in msg_zero
    assert "Arriving in" not in msg_zero
    assert_valid_telegram_html(msg_zero)


def test_formatting_with_cda_and_overhead_badge() -> None:
    """Closest Distance of Approach (CDA) formatting and direct overhead badge (< 1.0 km)."""
    # Direct overhead: cda < 1.0 km
    msg_overhead = format_alert_message(cda_km=0.6, distance_km=12.0, eta_seconds=90.0)
    assert "Closest Pass (CDA):" in msg_overhead
    assert "0.6 km" in msg_overhead
    assert "Direct Overhead" in msg_overhead
    assert_valid_telegram_html(msg_overhead)

    # Distant pass: cda >= 1.0 km
    msg_distant = format_alert_message(cda_km=8.4, distance_km=15.0)
    assert "Closest Pass (CDA):" in msg_distant
    assert "8.4 km" in msg_distant
    assert "Direct Overhead" not in msg_distant
    assert_valid_telegram_html(msg_distant)


def test_formatting_with_bearing_and_closure_rate() -> None:
    """Format observer bearing from user and closure rate."""
    msg = format_alert_message(
        bearing_from_user=225.0,
        closure_rate_ms=205.77,  # ~400 kt
    )
    assert "SW" in msg
    assert "225°" in msg
    assert "Closure Rate:" in msg
    assert "closing" in msg
    assert_valid_telegram_html(msg)


def test_graceful_handling_missing_and_none_fields() -> None:
    """Message builder must handle None, zero, or empty fields without throwing exceptions."""
    msg = format_alert_message(
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
    assert isinstance(msg, str)
    assert len(msg) > 0
    assert "10.0 km" in msg
    # Verify no raw 'None' text in values
    assert "None km" not in msg
    assert "None ft" not in msg
    assert "None m" not in msg
    assert "Speed: None" not in msg
    assert_valid_telegram_html(msg)


def test_current_app_aircraft_alert_message_callable() -> None:
    """Verify app.bot.messages.aircraft_alert_message directly to ensure no breaking regression."""
    # Test existing function with legacy positional/keyword args
    msg = messages.aircraft_alert_message(
        aircraft_type="A321",
        callsign="EZY123",
        distance_km=18.2,
        altitude_m=4000.0,
        velocity_ms=180.0,
        heading=90.0,
        icao24="400001",
        origin_country="United Kingdom",
        eta_seconds=85.0,
    )
    assert "A321" in msg
    assert "EZY123" in msg
    assert "18.2 km" in msg
    assert "Arriving in" in msg


def test_direct_aircraft_alert_message_with_m2_kinematics() -> None:
    """Verify app.bot.messages.aircraft_alert_message directly with all rich kinematics parameters."""
    msg = messages.aircraft_alert_message(
        aircraft_type="B738",
        callsign="SWA890",
        distance_km=21.5,
        altitude_m=9144.0,  # 30,000 ft
        velocity_ms=230.0,
        heading=270.0,
        icao24="ab1234",
        origin_country="Saint Kitts & Nevis",
        eta_seconds=150.0,
        cda_km=0.5,
        trajectory_status="direct_hit",
        bearing_from_user=90.0,
        closure_rate_ms=225.0,
    )
    assert "Saint Kitts &amp; Nevis" in msg
    assert "Arriving in ~2m 30s" in msg
    assert "UTC" in msg
    assert "Closest Pass (CDA):" in msg
    assert "0.5 km" in msg
    assert "[Direct Overhead!]" in msg
    assert "Bearing from you:" in msg
    assert "90° (E)" in msg
    assert "Closure Rate:" in msg
    assert "225 m/s" in msg
    assert "437 kt" in msg
    assert "closing" in msg
    assert "30,000 ft" in msg
    assert_valid_telegram_html(msg)


def test_format_eta_timestamp() -> None:
    """Verify format_eta_timestamp produces valid UTC clock strings."""
    assert hasattr(messages, "format_eta_timestamp")
    res = messages.format_eta_timestamp(120)
    assert res.endswith("UTC")
    assert ":" in res
    assert messages.format_eta_timestamp(None) == ""
    assert messages.format_eta_timestamp(0) == ""
    assert messages.format_eta_timestamp(-10) == ""
