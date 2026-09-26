"""User-facing external aircraft tracking links.

Plane Alerts does not call Flightradar24 as a runtime data provider.  These
helpers only build links that a Telegram user may choose to open.
"""
from __future__ import annotations

import re
from urllib.parse import quote

FLIGHTRADAR24_BASE_URL = "https://www.flightradar24.com"
FLIGHTRADAR24_DATA_URL = f"{FLIGHTRADAR24_BASE_URL}/data"


def _clean_callsign(value: str | None) -> str:
    """Return a URL-safe aircraft callsign token, or an empty string."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())[:24]


def _clean_icao24(value: str | None) -> str:
    """Return a normalized six-character ICAO24 address, or an empty string."""
    raw = str(value or "").strip().lower()
    return raw if re.fullmatch(r"[0-9a-f]{6}", raw) else ""


def flightradar24_url(*, callsign: str | None = None, icao24: str | None = None) -> str | None:
    """Build the safest available Flightradar24 link for an aircraft.

    Flightradar24 supports live-flight URLs keyed by callsign.  An ICAO24 hex
    address is useful in Flightradar24's Data search but is not itself a stable
    live-flight URL slug, so ICAO-only observations fall back to the Data page
    rather than fabricating an aircraft-specific URL.
    """
    clean_icao = _clean_icao24(icao24)
    clean_callsign = _clean_callsign(callsign)

    # Some internal fallbacks use the ICAO24 address as the displayed callsign.
    # Do not treat that value as a real Flightradar24 callsign deep-link.
    if clean_callsign and clean_callsign.lower() != clean_icao:
        return f"{FLIGHTRADAR24_BASE_URL}/{quote(clean_callsign, safe='')}"
    if clean_icao:
        return FLIGHTRADAR24_DATA_URL
    return None
