"""Pre-formatted message templates for the Telegram bot.

All bot-facing text lives here so it's easy to localise or tweak wording
without touching handler logic.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone

from app.aircraft.categories import CATEGORY_EMOJIS, get_all_types_for_categories
from app.worker.geo import (
    heading_to_cardinal,
    km_to_nautical_miles,
    metres_to_feet,
    ms_to_knots,
)


# ── Welcome & Disclaimer ────────────────────────────────────────────────────

WELCOME_MESSAGE = (
    "✈️ <b>Welcome to Aircraft Alert Bot!</b>\n"
    "\n"
    "I'll notify you when interesting aircraft fly near your location.\n"
    "\n"
    "📋 Before we start, please read our disclaimer:\n"
    "\n"
    "⚠️ <b>IMPORTANT DISCLAIMER</b>\n"
    "• Aircraft data may be delayed or incomplete\n"
    "• Coverage varies by region (best in US/Europe)\n"
    "• Some military aircraft don't transmit ADS-B\n"
    "• Data providers may have outages\n"
    "• This is for informational purposes only\n"
    "• Never rely solely on this service\n"
    "• Always use common sense\n"
    "\n"
    "By continuing, you acknowledge these limitations."
)

# ── Setup Steps ──────────────────────────────────────────────────────────────

LOCATION_PROMPT = (
    "📍 <b>Step 1: Send your location</b>\n"
    "\n"
    "This is where I'll monitor for aircraft.\n"
    "Your location is stored securely and only used for alerts.\n"
    "\n"
    "<b>How to send location:</b>\n"
    "1. Tap the 📎 attachment icon\n"
    "2. Select <b>Location</b>\n"
    "3. Choose <b>Send My Current Location</b> or pick manually"
)

LOCATION_SAVED = (
    "✅ <b>Location saved!</b>\n"
    "📍 Coordinates: <code>{lat:.4f}</code>, <code>{lon:.4f}</code>\n"
)

RADIUS_PROMPT = (
    "🎯 <b>Step 2: Set Monitoring Radius</b>\n"
    "\n"
    "How many kilometers around your location would you like to monitor?\n"
    "\n"
    "Please type a number between <b>1</b> and <b>150</b> (e.g., <code>15</code>, <code>20</code>, or <code>50</code>)."
)

INVALID_RADIUS = (
    "❌ <b>Invalid radius.</b>\n"
    "\n"
    "Please enter a valid number between <b>1</b> and <b>150</b>."
)

AIRCRAFT_SELECTION_PROMPT = (
    "✈️ <b>Step 3: Choose aircraft to monitor</b>\n"
    "\n"
    "Select categories below. Tap to toggle.\n"
    "You can also add custom ICAO type codes.\n"
    "\n"
    "When you're done, tap <b>✅ Done</b>."
)

CUSTOM_AIRCRAFT_PROMPT = (
    "✏️ <b>Add Custom Aircraft Type</b>\n"
    "\n"
    "Send me an ICAO type code (2-4 characters).\n"
    "Examples: <code>B38M</code>, <code>A21N</code>, <code>E190</code>\n"
    "\n"
    "Send <code>done</code> when finished, or tap /cancel."
)

INVALID_ICAO_CODE = (
    "❌ <code>{code}</code> doesn't look like a valid ICAO type code.\n"
    "It must be 2-4 alphanumeric characters.\n"
    "\n"
    "Try again or send <code>done</code> to finish."
)

CUSTOM_AIRCRAFT_ADDED = (
    "✅ Added <code>{code}</code> to your custom types.\n"
    "Send another code, or <code>done</code> to finish."
)

NO_CATEGORIES_SELECTED = (
    "⚠️ Please select at least one aircraft category or add a custom type "
    "before finishing setup."
)


# ── Setup Complete ───────────────────────────────────────────────────────────

def setup_complete_message(
    selected_categories: list[str],
    custom_aircraft: list[str],
    lat: float,
    lon: float,
    radius_km: float,
) -> str:
    """Build the setup-complete summary message."""
    lines = ["🎉 <b>Setup complete!</b>\n"]

    # Monitoring targets
    lines.append("<b>Monitoring for:</b>")
    for cat in selected_categories:
        emoji = CATEGORY_EMOJIS.get(cat, "✈️")
        lines.append(f"  {emoji} {cat}")

    total_types = len(get_all_types_for_categories(selected_categories))
    if custom_aircraft:
        custom_str = ", ".join(f"<code>{c}</code>" for c in custom_aircraft)
        lines.append(f"  ✏️ Custom: {custom_str}")
        total_types += len(custom_aircraft)

    lines.append(f"\n<b>Total type codes tracked:</b> {total_types}")

    # Location
    lines.append(f"\n📍 <b>Location:</b> <code>{lat:.4f}</code>, <code>{lon:.4f}</code>")
    lines.append(f"📏 <b>Radius:</b> {radius_km:.0f} km")

    # Commands
    lines.append(
        "\n<b>Commands:</b>\n"
        "/setup — Change all preferences\n"
        "/location — Update location\n"
        "/preferences — Update aircraft types\n"
        "/status — View current config\n"
        "/help — Show all commands"
    )
    return "\n".join(lines)


# ── Status ───────────────────────────────────────────────────────────────────

def status_message(
    selected_categories: list[str],
    custom_aircraft: list[str],
    lat: float | None,
    lon: float | None,
    radius_km: float,
    setup_complete: bool,
) -> str:
    """Build the /status response."""
    if not setup_complete:
        return (
            "⚙️ <b>Status</b>\n\n"
            "You haven't completed setup yet.\n"
            "Use /start to begin."
        )

    lines = ["⚙️ <b>Current Configuration</b>\n"]

    # Categories
    lines.append("<b>Monitored categories:</b>")
    if selected_categories:
        for cat in selected_categories:
            emoji = CATEGORY_EMOJIS.get(cat, "✈️")
            lines.append(f"  {emoji} {cat}")
    else:
        lines.append("  (none)")

    if custom_aircraft:
        custom_str = ", ".join(f"<code>{c}</code>" for c in custom_aircraft)
        lines.append(f"\n<b>Custom types:</b> {custom_str}")

    total_types = len(get_all_types_for_categories(selected_categories)) + len(custom_aircraft)
    lines.append(f"\n<b>Total type codes:</b> {total_types}")

    # Location
    if lat is not None and lon is not None:
        lines.append(f"\n📍 <b>Location:</b> <code>{lat:.4f}</code>, <code>{lon:.4f}</code>")
    else:
        lines.append("\n📍 <b>Location:</b> Not set")

    lines.append(f"📏 <b>Radius:</b> {radius_km:.0f} km")
    lines.append("\n✅ <b>Monitoring active</b>")

    return "\n".join(lines)


# ── Notifications ────────────────────────────────────────────────────────────

def format_duration(seconds: float | int | None) -> str:
    """Format duration into human-readable string (e.g., '45s', '2m 15s', '< 10s')."""
    if seconds is None or not math.isfinite(seconds):
        return ""
    total_sec = max(0, int(round(seconds)))
    if total_sec < 10:
        return "< 10s"
    if total_sec < 60:
        return f"{total_sec}s"
    minutes, sec = divmod(total_sec, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s" if sec > 0 else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes > 0 else f"{hours}h"


def format_eta_timestamp(seconds: float | int | None, ref_time: datetime | None = None) -> str:
    """Format arrival clock time in UTC (e.g., '14:32 UTC')."""
    if seconds is None or not math.isfinite(seconds) or seconds <= 0:
        return ""
    base = ref_time or datetime.now(timezone.utc)
    arrival = base + timedelta(seconds=seconds)
    return arrival.strftime("%H:%M UTC")


def aircraft_alert_message(
    aircraft_type: str,
    callsign: str,
    distance_km: float,
    altitude_m: float | None = None,
    velocity_ms: float | None = None,
    heading: float | None = None,
    icao24: str = "",
    origin_country: str = "",
    eta_seconds: float | None = None,
    cda_km: float | None = None,
    trajectory_status: str | None = None,
    bearing_from_user: float | None = None,
    closure_rate_ms: float | None = None,
) -> str:
    """Format an aircraft notification message with rich kinematics and HTML escaping.

    Guarantees strict HTML escaping on dynamic strings (callsign, origin_country, aircraft_type)
    and graceful degradation when fields are None.
    """
    safe_type = html.escape(aircraft_type or "Unknown", quote=True)
    safe_callsign = html.escape(callsign.strip(), quote=True) if callsign else ""
    safe_origin = html.escape(origin_country.strip(), quote=True) if origin_country else ""
    safe_icao = html.escape(icao24.strip().lower(), quote=True) if icao24 else ""

    lines: list[str] = []

    # 1. Header & ETA
    if eta_seconds is not None and eta_seconds > 0:
        duration_str = html.escape(format_duration(eta_seconds), quote=True)
        time_str = format_eta_timestamp(eta_seconds)
        eta_desc = f"Arriving in ~{duration_str}"
        if time_str:
            eta_desc += f" ({time_str})"
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
    lines.append(f"\n📍 <b>Distance:</b> {distance_km:.1f} km ({dist_nm:.1f} NM) away")

    if bearing_from_user is not None:
        cardinal = heading_to_cardinal(bearing_from_user)
        lines.append(f"🧭 <b>Bearing from you:</b> {bearing_from_user:.0f}° ({cardinal})")

    if cda_km is not None:
        cda_nm = km_to_nautical_miles(cda_km)
        if cda_km < 1.0:
            lines.append(f"🎯 <b>Closest Pass (CDA):</b> {cda_km:.1f} km ({cda_nm:.1f} NM) [Direct Overhead!]")
        else:
            lines.append(f"🎯 <b>Closest Pass (CDA):</b> {cda_km:.1f} km ({cda_nm:.1f} NM)")

    if trajectory_status:
        lines.append(f"🧭 <b>Trajectory:</b> {html.escape(str(trajectory_status), quote=True)}")

    if closure_rate_ms is not None:
        closure_rate_kt = ms_to_knots(closure_rate_ms)
        suffix = " closing" if closure_rate_ms > 1.0 else (" receding" if closure_rate_ms < -1.0 else "")
        lines.append(f"⚡ <b>Closure Rate:</b> {closure_rate_ms:.0f} m/s (~{closure_rate_kt:.0f} kt){suffix}")

    # 4. Telemetry
    telemetry: list[str] = []
    if altitude_m is not None:
        alt_ft = metres_to_feet(altitude_m)
        telemetry.append(f"<b>Altitude:</b> {alt_ft:,} ft ({altitude_m:,.0f} m)")

    if velocity_ms is not None:
        speed_kt = ms_to_knots(velocity_ms)
        speed_kmh = round(velocity_ms * 3.6)
        telemetry.append(f"<b>Speed:</b> {speed_kt} kt ({speed_kmh} km/h)")

    if heading is not None:
        hdg_cardinal = heading_to_cardinal(heading)
        telemetry.append(f"<b>Heading:</b> {hdg_cardinal} ({heading:.0f}°)")

    if telemetry:
        lines.append("\n" + "\n".join(telemetry))

    # 5. Tracking Link
    if safe_icao:
        lines.append(f'\n<a href="https://globe.adsb.fi/?icao={safe_icao}">🌍 Track Live on ADSB.fi</a>')

    return "\n".join(lines)


# ── Help ─────────────────────────────────────────────────────────────────────

HELP_MESSAGE = (
    "📖 <b>Aircraft Alert Bot — Help</b>\n"
    "\n"
    "<b>Setup &amp; Configuration</b>\n"
    "/start — Initial welcome &amp; setup\n"
    "/setup — Re-run full setup (overwrites config)\n"
    "/location — Update your monitoring location\n"
    "/preferences — Change aircraft type selection\n"
    "\n"
    "<b>Information</b>\n"
    "/status — View your current configuration\n"
    "/help — Show this help message\n"
    "\n"
    "<b>How it works</b>\n"
    "I check for aircraft near your location every ~45 seconds. "
    "When an aircraft matching your preferences is detected within "
    "your monitoring radius, you'll receive an alert.\n"
    "\n"
    "Each aircraft will only alert once every 30 minutes to avoid spam.\n"
    "\n"
    "<b>Tip:</b> You can add custom ICAO type codes during setup or via "
    "/preferences to track specific aircraft types not in the preset categories."
)

CANCEL_MESSAGE = "❌ Operation cancelled. Use /help to see available commands."
ALREADY_SETUP_MESSAGE = (
    "You've already completed setup. Use /setup to reconfigure, "
    "or /status to view your current settings."
)
