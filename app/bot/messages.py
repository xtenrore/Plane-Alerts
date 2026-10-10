"""Pre-formatted message templates for the Telegram bot.

All bot-facing text lives here so it's easy to localise or tweak wording
without touching handler logic.
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

from app.aircraft.categories import CATEGORY_EMOJIS, get_all_types_for_categories
from app.bot.flight_links import flightradar24_url
from app.worker.geo import heading_to_cardinal, km_to_nautical_miles, metres_to_feet, ms_to_knots


# ── Welcome & Disclaimer ────────────────────────────────────────────────────

WELCOME_MESSAGE = (
    "✈️ <b>Welcome to Plane Alerts!</b>\n"
    "\n"
    "I'll notify you when matching aircraft are genuinely projected to pass near your spotting location.\n"
    "\n"
    "📋 Before we start, please read our disclaimer:\n"
    "\n"
    "⚠️ <b>IMPORTANT DISCLAIMER</b>\n"
    "• Aircraft data may be delayed or incomplete\n"
    "• Coverage varies by region\n"
    "• Some aircraft may not transmit usable ADS-B data\n"
    "• Data providers may have outages\n"
    "• This is for informational and spotting purposes only\n"
    "• Never rely solely on this service\n"
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
    lines = ["🎉 <b>Setup complete — Plane Alerts!</b>\n"]

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
    lines.append(f"\n📍 <b>Location:</b> <code>{lat:.4f}</code>, <code>{lon:.4f}</code>")
    lines.append(f"📏 <b>Radius:</b> {radius_km:.0f} km")
    lines.append(
        "\n<b>Useful commands:</b>\n"
        "/next60 — Next 60 Minutes forecast\n"
        "/forecast — Alias for /next60\n"
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

    lines = ["⚙️ <b>Plane Alerts Configuration</b>\n"]
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

    if lat is not None and lon is not None:
        lines.append(f"\n📍 <b>Location:</b> <code>{lat:.4f}</code>, <code>{lon:.4f}</code>")
    else:
        lines.append("\n📍 <b>Location:</b> Not set")

    lines.append(f"📏 <b>Radius:</b> {radius_km:.0f} km")
    lines.append("\n✅ <b>Monitoring active</b>")
    return "\n".join(lines)


# ── Notifications ────────────────────────────────────────────────────────────

def format_duration(seconds: float | int | None) -> str:
    """Format duration in seconds into human-readable duration string."""
    if seconds is None:
        return ""
    sec = float(seconds)
    if sec < 10.0:
        return "< 10s"
    if sec < 60.0:
        return f"{int(round(sec))}s"
    if sec < 3600.0:
        m, s = divmod(int(round(sec)), 60)
        return f"{m}m" if s == 0 else f"{m}m {s}s"
    h, rem = divmod(int(round(sec)), 3600)
    m = rem // 60
    return f"{h}h" if m == 0 else f"{h}h {m}m"


def format_eta_timestamp(seconds: float | int | None, ref_time: datetime | None = None) -> str:
    """Format ETA duration in seconds as UTC arrival clock timestamp (HH:MM UTC)."""
    if seconds is None:
        return ""
    ref = ref_time if ref_time is not None else datetime.now(timezone.utc)
    target = ref + timedelta(seconds=max(0.0, float(seconds)))
    return target.strftime("%H:%M UTC")


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
    """Format a rich, HTML-escaped aircraft notification message."""
    safe_type = html.escape(aircraft_type or "Unknown", quote=True)
    safe_callsign = html.escape(callsign.strip(), quote=True) if callsign else ""
    safe_origin = html.escape(origin_country.strip(), quote=True) if origin_country else ""
    safe_icao = html.escape(icao24.strip().lower(), quote=True) if icao24 else ""

    lines: list[str] = []

    if eta_seconds is not None and eta_seconds > 0:
        duration_raw = format_duration(eta_seconds)
        time_raw = format_eta_timestamp(eta_seconds)
        safe_duration = html.escape(duration_raw, quote=True)
        safe_time = html.escape(time_raw, quote=True) if time_raw else ""
        time_suffix = f" ({safe_time})" if safe_time else ""
        lines.append(f"🚀 <b>Early Warning Alert!</b> (Arriving in ~{safe_duration}{time_suffix})\n")
    else:
        lines.append("✈️ <b>Aircraft Alert!</b>\n")

    lines.append(f"<b>Type:</b> <code>{safe_type}</code>")
    if safe_callsign:
        lines.append(f"<b>Callsign:</b> <code>{safe_callsign}</code>")
    lines.append(f"<b>Distance:</b> {distance_km:.1f} km away")

    if cda_km is not None:
        cda_nm = km_to_nautical_miles(cda_km)
        badge = " [Direct Overhead!]" if cda_km < 1.0 else ""
        lines.append(f"🎯 <b>Closest Pass (CDA):</b> {cda_km:.1f} km ({cda_nm:.1f} NM){badge}")

    if bearing_from_user is not None:
        cardinal_dir = heading_to_cardinal(bearing_from_user)
        lines.append(f"👁️ <b>Bearing:</b> {cardinal_dir} ({bearing_from_user:.0f}° from you)")

    if closure_rate_ms is not None:
        closure_kt = ms_to_knots(abs(closure_rate_ms))
        direction = " closing" if closure_rate_ms > 1.0 else (" receding" if closure_rate_ms < -1.0 else "")
        lines.append(f"⚡ <b>Closure Rate:</b> {abs(closure_rate_ms):.0f} m/s ({closure_kt} kt){direction}")

    if trajectory_status:
        safe_traj = html.escape(str(trajectory_status), quote=True)
        lines.append(f"🧭 <b>Trajectory:</b> {safe_traj}")

    if altitude_m is not None:
        alt_ft = metres_to_feet(altitude_m)
        lines.append(f"<b>Altitude:</b> {altitude_m:,.0f} m ({alt_ft:,} ft)")

    if velocity_ms is not None:
        speed_kt = ms_to_knots(velocity_ms)
        lines.append(f"<b>Speed:</b> {velocity_ms:.0f} m/s ({speed_kt} kt)")

    if heading is not None:
        cardinal = heading_to_cardinal(heading)
        lines.append(f"<b>Heading:</b> {cardinal} ({heading:.0f}°)")

    if safe_origin:
        lines.append(f"\n<b>Origin:</b> {safe_origin}")

    tracker_url = flightradar24_url(callsign=callsign, icao24=icao24)
    if tracker_url:
        lines.append(f'\n<a href="{tracker_url}">🌍 Track on Flightradar24</a>')
    return "\n".join(lines)


# ── Help ─────────────────────────────────────────────────────────────────────

HELP_MESSAGE = (
    "📖 <b>Plane Alerts v4.0 — Help</b>\n"
    "\n"
    "<b>Forecast &amp; spotting</b>\n"
    "/next60 — Planes expected in the next 60 minutes\n"
    "/forecast — Alias for /next60\n"
    "/spotting — Open Spotting Mode\n"
    "/photo — Current shooting guidance\n"
    "/conditions — Weather / sun / haze conditions\n"
    "\n"
    "<b>Setup &amp; configuration</b>\n"
    "/start — Initial setup\n"
    "/setup — Re-run setup\n"
    "/location — Update monitoring location\n"
    "/preferences — Change aircraft selection\n"
    "/camera — Set camera body\n"
    "/lens — Set lens\n"
    "\n"
    "<b>Information</b>\n"
    "/status — View your current configuration\n"
    "/help — Show this help message\n"
    "\n"
    "Plane Alerts uses live trajectory/CPA when available. Longer 30–60 minute forecast entries are shadow/history-based and become more precise only as the aircraft gets closer."
)

CANCEL_MESSAGE = "❌ Operation cancelled. Use /help to see available commands."
ALREADY_SETUP_MESSAGE = (
    "You've already completed setup. Use /setup to reconfigure, "
    "or /status to view your current settings."
)
