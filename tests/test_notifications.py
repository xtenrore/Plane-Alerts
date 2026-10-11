"""Regression tests for Telegram notification safety and lifecycle alerts."""

from types import SimpleNamespace

from app.aircraft.models import NormalizedAircraft
from app.worker.notifications import _approach_text, _basic_alert_text, _safe_provider_text
from tests.test_messages import StrictTelegramHTMLValidator


def test_provider_text_is_safe_for_telegram_html():
    assert _safe_provider_text("A&B <test>") == "A&amp;B &lt;test&gt;"
    assert _safe_provider_text('callsign "quoted"') == "callsign &quot;quoted&quot;"


def _make_ac(*, callsign="THY123", typ="A359", alt=1200.0, gs=240.0, hdg=90.0):
    return NormalizedAircraft(
        icao24="4b1234",
        callsign=callsign,
        latitude=41.1,
        longitude=29.0,
        altitude=alt,
        velocity=gs * 0.514444,
        heading=hdg,
        vertical_rate_mps=-3.0,
        on_ground=False,
        squawk="1000",
        aircraft_type=typ,
        origin_country="Turkey",
    )


def _make_pred(*, cpa=0.5, current=8.0, t_cpa=135.0, entry=45.0, trend=-0.12):
    return SimpleNamespace(
        state="Approaching",
        confidence="High",
        projected_closest_km=cpa,
        current_distance_km=current,
        time_to_cpa_s=t_cpa,
        radius_entry_s=entry,
        distance_trend_km_s=trend,
    )


def test_approach_text_next_shot_lifecycle():
    """NEXT SHOT (prepare) contains UTC ETA, perimeter entry, and CPA."""
    ac = _make_ac()
    pred = _make_pred(cpa=2.5, current=15.0, t_cpa=180.0, entry=60.0)
    text = _approach_text(ac, pred, stage="prepare")
    assert "📡 <b>NEXT SHOT</b>" in text
    assert "<b>THY123</b>" in text
    assert "<code>A359</code>" in text
    assert "⏱ <b>03:00</b>" in text
    assert "UTC)" in text
    assert "CPA <b>2.5 km</b>" in text
    assert "🎯 Perimeter entry in ~1m" in text
    assert "⚡ 120 m/s (233 kt) closing" in text
    assert "E (90°)" in text
    StrictTelegramHTMLValidator().validate(text)


def test_approach_text_camera_ready_lifecycle():
    """CAMERA READY stage displays camera recommendations and countdown."""
    ac = _make_ac()
    pred = _make_pred(cpa=1.2, current=5.0, t_cpa=45.0)
    cam = SimpleNamespace(
        shutter_speed="1/2000s",
        aperture="f/8",
        iso="ISO 200",
        focal_length="400mm",
        best_window_start_s=40.0,
    )
    text = _approach_text(ac, pred, stage="camera_ready", camera=cam)
    assert "📷 <b>CAMERA READY</b>" in text
    assert "⏱ <b>00:40</b>" in text
    assert "CPA <b>1.2 km</b>" in text
    assert "📷 1/2000s · f/8 · ISO 200 · 400mm" in text
    StrictTelegramHTMLValidator().validate(text)


def test_approach_text_photo_now_direct_overhead():
    """PHOTO NOW with CPA < 1.0 km includes [Direct Overhead!] badge."""
    ac = _make_ac(callsign="AFR456", typ="B77W")
    pred = _make_pred(cpa=0.4, current=1.2, t_cpa=10.0)
    text = _approach_text(ac, pred, stage="photo_now")
    assert "🔥 <b>PHOTO NOW</b>" in text
    assert "[Direct Overhead!]" in text
    assert "CPA <b>0.4 km</b> (0.2 NM)" in text
    StrictTelegramHTMLValidator().validate(text)

    # Uncertain prediction suppresses [Direct Overhead!] badge
    pred_uncertain = _make_pred(cpa=0.4, current=1.2, t_cpa=10.0)
    pred_uncertain.confidence = "Uncertain"
    text_unc = _approach_text(ac, pred_uncertain, stage="photo_now")
    assert "[Direct Overhead!]" not in text_unc
    StrictTelegramHTMLValidator().validate(text_unc)


def test_approach_text_passed_and_cancelled_stages():
    """PASSED and CANCELLED stages format cleanly."""
    ac = _make_ac()
    pred = _make_pred(cpa=3.0, current=9.0)
    passed_text = _approach_text(ac, pred, stage="passed", observed_closest_km=2.8)
    assert "✅ <b>AIRCRAFT PASSED</b>" in passed_text
    assert "Closest <b>2.8 km</b>" in passed_text
    StrictTelegramHTMLValidator().validate(passed_text)

    cancelled_text = _approach_text(ac, pred, stage="cancelled", previous_cpa_km=2.0)
    assert "↪️ <b>TRAJECTORY CHANGED</b>" in cancelled_text
    assert "Alert cancelled · CPA <b>3.0 km</b> (was 2.0 km)" in cancelled_text
    StrictTelegramHTMLValidator().validate(cancelled_text)


def test_basic_alert_text_formatted_duration_and_timestamp():
    """_basic_alert_text includes formatted duration and UTC clock timestamp."""
    ac = _make_ac(callsign="KLM789")
    text = _basic_alert_text(ac, distance_km=14.5, eta_seconds=135.0)
    assert "🚀 <b>EARLY WARNING</b>" in text
    assert "~2m 15s" in text
    assert "UTC)" in text
    assert "14.5 km away" in text
    assert "E (90°)" in text
    StrictTelegramHTMLValidator().validate(text)

    # Under 30s suppresses clock timestamp
    text_short = _basic_alert_text(ac, distance_km=5.0, eta_seconds=20.0)
    assert "~20s" in text_short
    assert "UTC)" not in text_short
    StrictTelegramHTMLValidator().validate(text_short)


def test_approach_text_adversarial_escaping():
    """Adversarial aircraft names with HTML tags are safely escaped in _approach_text."""
    ac = _make_ac(callsign="A<B>&C", typ="<b>JET</b>")
    pred = _make_pred()
    text = _approach_text(ac, pred, stage="prepare")
    assert "<B>" not in text
    assert "<b>JET</b>" not in text
    assert "A&lt;B&gt;&amp;C" in text
    assert "&lt;B&gt;JET&lt;/B&gt;" in text
    StrictTelegramHTMLValidator().validate(text)


def test_approach_and_basic_sub_ten_second_html_escaping():
    """Durations under 10 seconds return '< 10s' which must be escaped as '&lt; 10s'."""
    ac = _make_ac(callsign="AFR101")
    pred = _make_pred(entry=6.0, t_cpa=40.0)
    text = _approach_text(ac, pred, stage="prepare")
    assert "&lt; 10s" in text
    assert "< 10s" not in text
    StrictTelegramHTMLValidator().validate(text)

    basic = _basic_alert_text(ac, distance_km=12.0, eta_seconds=5.0)
    assert "&lt; 10s" in basic
    assert "< 10s" not in basic
    StrictTelegramHTMLValidator().validate(basic)


def test_duck_typed_aircraft_without_ground_speed():
    """Duck-typed aircraft with only velocity (no ground_speed property) format cleanly."""
    ac = SimpleNamespace(
        icao24="4b9999",
        callsign="MOCK1",
        aircraft_type="B738",
        altitude=3000.0,
        velocity=200.0,
        heading=180.0,
    )
    pred = _make_pred(cpa=1.5, current=8.0, t_cpa=35.0)
    text = _approach_text(ac, pred, stage="prepare")
    assert "389 kt" in text
    StrictTelegramHTMLValidator().validate(text)

    basic = _basic_alert_text(ac, distance_km=8.0, eta_seconds=30.0)
    assert "389 kt" in basic
    StrictTelegramHTMLValidator().validate(basic)
