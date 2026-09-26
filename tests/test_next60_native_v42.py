from datetime import datetime, timedelta, timezone
import inspect

from app.bot import next60


def _doc(**overrides):
    now = datetime(2026, 9, 18, 19, 0, tzinfo=timezone.utc)
    base = {
        "callsign": "THY1VP",
        "aircraft_icao24": "4bbf47",
        "aircraft_type": "B789",
        "predicted_cpa_at": now + timedelta(minutes=40),
        "predicted_closest_km": 7.4,
        "confidence": "High",
        "stage": "prepare",
        "source": "live",
    }
    base.update(overrides)
    return now, base


def test_native_next60_compact_line_and_buttons():
    now, doc = _doc()
    text, markup = next60.render_next60_native(now, [doc])

    assert "B789" in text
    assert "THY1VP" in text
    assert "40m" in text
    assert "7.4 km" in text
    assert markup is not None

    row = markup.inline_keyboard[0]
    assert row[0].text == "FR24 · THY1VP"
    assert row[0].url == "https://www.flightradar24.com/THY1VP"
    assert row[1].text == "More Info · THY1VP"
    assert row[1].callback_data == "n60_more:THY1VP"


def test_history_shadow_without_icao_still_has_flightradar24_callsign_target():
    now, doc = _doc(
        aircraft_icao24=None,
        aircraft_type=None,
        source="history",
        historical_days=2,
    )
    text, markup = next60.render_next60_native(now, [doc])

    assert "THY1VP" in text
    assert markup is not None
    row = markup.inline_keyboard[0]
    assert len(row) == 2
    assert row[0].text == "FR24 · THY1VP"
    assert row[0].url == "https://www.flightradar24.com/THY1VP"
    assert row[1].text == "More Info · THY1VP"


def test_more_info_contains_deterministic_forecast_fields():
    now, doc = _doc()
    text = next60._detail_text(now, doc)

    assert "Aircraft: <b>B789</b>" in text
    assert "ETA to closest approach: <b>40m</b>" in text
    assert "Projected closest: <b>7.4 km</b>" in text
    assert "Confidence: <b>High</b>" in text
    assert "Live deterministic trajectory" in text
    assert "4BBF47" in text


def test_next60_command_no_longer_uses_webapp():
    source = inspect.getsource(next60)
    assert "WebAppInfo" not in source
    assert "Open Next 60" not in source
    assert "_next60_web_app_url" not in source
