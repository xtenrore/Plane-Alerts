"""Tests for ADS-B data providers and deduplication."""

import time

from app.aircraft.models import NormalizedAircraft
from app.aircraft.providers import ProviderManager, _feet_to_metres, _knots_to_ms, parse_adsb_response


def test_unit_conversions():
    assert _feet_to_metres(1000) == 304.8
    assert _feet_to_metres(None) is None
    assert _feet_to_metres("invalid") is None
    assert _knots_to_ms(100) == 51.4
    assert _knots_to_ms(None) is None
    assert _knots_to_ms("invalid") is None


def test_parse_adsb_response():
    raw_data = {"ac": [{"hex": "40082c", "flight": "VIR105 ", "lat": 51.47, "lon": -0.45, "alt_baro": 35000, "gs": 480.5, "track": 280.0, "t": "B789", "seen_pos": 1700000000}, {"hex": "aabbcc", "flight": "MALFORMED"}]}
    aircraft = parse_adsb_response(raw_data)
    assert len(aircraft) == 2
    assert aircraft[0].icao24 == "40082c"
    assert aircraft[0].callsign == "VIR105"
    assert aircraft[0].aircraft_type == "B789"
    assert aircraft[0].altitude == 10668.0
    assert aircraft[0].has_position is True
    assert aircraft[1].icao24 == "aabbcc"
    assert aircraft[1].has_position is False


def test_provider_manager_merge_and_type_cache():
    pm = ProviderManager(); now = time.time()
    ac1 = NormalizedAircraft(icao24="400111", callsign="BAW1", latitude=51.5, longitude=-0.1, aircraft_type="B738", position_age_s=1.0, timestamp=now-1.0)
    ac2 = NormalizedAircraft(icao24="400111", callsign="BAW1", latitude=51.501, longitude=-0.101, aircraft_type="", position_age_s=2.0, timestamp=now-2.0)
    merged = pm._merge_results({"adsb.lol": [ac1], "adsb.fi": [ac2]})
    assert len(merged) == 1
    assert merged[0].icao24 == "400111"
    assert merged[0].aircraft_type == "B738"
    assert "400111" in pm._type_cache
