from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.bot.flight_links import FLIGHTRADAR24_DATA_URL, flightradar24_url
from app.bot.messages import aircraft_alert_message
from app.bot.next60 import render_next60_native
from app.photography.keyboards import notification_actions_keyboard


def test_callsign_builds_direct_flightradar24_live_url() -> None:
    assert flightradar24_url(callsign=" fji 811 ", icao24="c083bd") == "https://www.flightradar24.com/FJI811"


def test_icao_only_falls_back_to_flightradar24_data_search_page() -> None:
    assert flightradar24_url(callsign="4BB0E6", icao24="4bb0e6") == FLIGHTRADAR24_DATA_URL
    assert flightradar24_url(icao24="4bb0e6") == FLIGHTRADAR24_DATA_URL
    assert flightradar24_url(callsign="", icao24="not-hex") is None


def test_notification_keyboard_uses_flightradar24_callsign_deep_link() -> None:
    keyboard = notification_actions_keyboard("n-1", "4bb0e6", "THY2SE")
    button = keyboard.inline_keyboard[0][0]
    assert button.text == "Open in Flightradar24"
    assert button.url == "https://www.flightradar24.com/THY2SE"
    assert "adsb.fi" not in button.url
    assert "adsb.lol" not in button.url


def test_legacy_aircraft_message_uses_flightradar24() -> None:
    text = aircraft_alert_message(
        aircraft_type="A359",
        callsign="THY2SE",
        distance_km=8.4,
        altitude_m=3500.0,
        velocity_ms=200.0,
        heading=270.0,
        icao24="4bb0e6",
        origin_country="Türkiye",
    )
    assert 'href="https://www.flightradar24.com/THY2SE"' in text
    assert "Track on Flightradar24" in text
    assert "adsb.fi" not in text


def test_next60_buttons_use_flightradar24_and_keep_more_info() -> None:
    now = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    docs = [{
        "callsign": "THY2SE",
        "aircraft_icao24": "4bb0e6",
        "aircraft_type": "A359",
        "predicted_cpa_at": now + timedelta(minutes=12),
        "predicted_closest_km": 4.2,
        "confidence": "High",
        "stage": "prepare",
        "source": "live",
    }]
    _, keyboard = render_next60_native(now, docs)
    assert keyboard is not None
    row = keyboard.inline_keyboard[0]
    assert row[0].text == "FR24 · THY2SE"
    assert row[0].url == "https://www.flightradar24.com/THY2SE"
    assert row[1].text == "More Info · THY2SE"
    assert row[1].callback_data == "n60_more:THY2SE"
    assert "adsb.fi" not in row[0].url
    assert "adsb.lol" not in row[0].url
