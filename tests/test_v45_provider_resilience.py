"""Regression coverage for Plane Alerts v4.5 provider resilience."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import app.aircraft.providers as providers_mod
from app.aircraft.models import NormalizedAircraft
from app.aircraft.providers import (
    AircraftDataProvider,
    MalformedProviderResponse,
    ProviderManager,
    ProviderRateLimited,
    opensky_bounding_boxes,
    parse_adsb_response,
    parse_local_receiver_response,
)
from app.config import Settings, settings


def _obs(source: str, *, icao: str = "4b1801", age: float = 1.0, lat: float = 41.0, lon: float = 29.0, callsign: str = "", aircraft_type: str = "", altitude: float | None = 10000.0, velocity: float | None = 220.0, heading: float | None = 270.0) -> NormalizedAircraft:
    now = 2_000_000_000.0
    return NormalizedAircraft(icao24=icao, callsign=callsign, latitude=lat, longitude=lon, altitude=altitude, velocity=velocity, heading=heading, aircraft_type=aircraft_type, position_age_s=age, source_freshness_s=age, timestamp=now-age, observed_at=now-age, received_at=now, source=source, field_provenance={"latitude": source, "longitude": source, **({"callsign": source} if callsign else {}), **({"aircraft_type": source} if aircraft_type else {})}, source_candidates=[source])


def test_canonical_timestamp_and_provenance_are_retained():
    now = time.time()
    items = parse_adsb_response({"now": now, "ac": [{"hex": "4b1801", "flight": "SWR123 ", "lat": 41.1, "lon": 29.2, "seen_pos": 2.5, "gs": 430, "track": 275}]}, source="adsb.lol")
    assert len(items) == 1
    ac = items[0]
    assert ac.source == "adsb.lol"
    assert ac.timestamp == pytest.approx(ac.observed_at)
    assert ac.observed_at == pytest.approx(now - 2.5, abs=0.1)
    assert ac.received_at is not None
    assert ac.position_age_s == pytest.approx(2.5, abs=0.2)
    assert ac.field_provenance["latitude"] == "adsb.lol"
    assert ac.field_provenance["callsign"] == "adsb.lol"


def test_missing_position_age_remains_missing():
    items = parse_local_receiver_response({"aircraft": [{"hex": "4b1801", "lat": 41.0, "lon": 29.0}]})
    assert len(items) == 1
    assert items[0].position_age_s is None
    assert items[0].observed_at is None
    assert items[0].timestamp is None


def test_local_readsb_dump1090_shape_and_missing_fields():
    items = parse_local_receiver_response({"now": time.time(), "messages": 1234, "aircraft": [{"hex": "4b1801", "flight": "THY7 ", "lat": 41.02, "lon": 28.94, "seen_pos": 0.4, "alt_baro": 12000, "gs": 355, "track": 301.2, "baro_rate": -832, "t": "A359"}, {"hex": "4b1802", "seen": 0.2}]})
    assert len(items) == 2
    assert items[0].source == "local"
    assert items[0].icao24 == "4b1801"
    assert items[0].callsign == "THY7"
    assert items[0].aircraft_type == "A359"
    assert items[0].altitude == pytest.approx(3657.6)
    assert items[1].has_position is False


def test_malformed_local_payload_is_explicit():
    with pytest.raises(MalformedProviderResponse):
        parse_local_receiver_response({"now": time.time(), "aircraft": "not-a-list"})


def test_malformed_coordinates_are_dropped():
    data = {"now": time.time(), "ac": [{"hex": "badlat", "lat": 95, "lon": 29, "seen_pos": 1}, {"hex": "badlon", "lat": 41, "lon": 181, "seen_pos": 1}, {"hex": "nan", "lat": "nan", "lon": 29, "seen_pos": 1}, {"hex": "good", "lat": 41, "lon": 29, "seen_pos": 1}]}
    assert [ac.icao24 for ac in parse_adsb_response(data)] == ["good"]


@pytest.mark.parametrize(("latitude", "longitude"), [(0.0, 0.0), (35.0, 10.0), (41.0082, 28.9784), (70.0, 20.0)])
def test_opensky_bounds_are_valid_across_latitudes(latitude, longitude):
    boxes = opensky_bounding_boxes(latitude, longitude, 250)
    assert boxes
    for box in boxes:
        assert -90 <= box["lamin"] <= box["lamax"] <= 90
        assert -180 <= box["lomin"] <= box["lomax"] <= 180


def test_opensky_longitude_scaling_increases_with_latitude():
    equator = opensky_bounding_boxes(0, 0, 120)[0]
    istanbul = opensky_bounding_boxes(41.0, 29.0, 120)[0]
    high = opensky_bounding_boxes(70.0, 20.0, 120)[0]
    assert (equator["lomax"] - equator["lomin"]) < (istanbul["lomax"] - istanbul["lomin"]) < (high["lomax"] - high["lomin"])


@pytest.mark.parametrize("longitude", [179.0, -179.0])
def test_opensky_date_line_crossing_splits_boxes(longitude):
    boxes = opensky_bounding_boxes(20.0, longitude, 250)
    assert len(boxes) == 2
    assert all(-180 <= b["lomin"] <= b["lomax"] <= 180 for b in boxes)


def test_opensky_near_pole_uses_global_longitude_without_invalid_values():
    boxes = opensky_bounding_boxes(89.5, 170.0, 250)
    assert len(boxes) == 1
    assert boxes[0]["lomax"] == 180.0
    assert boxes[0]["lomin"] == -180.0
    assert boxes[0]["lamax"] == 90.0


def test_local_receiver_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_url", "")
    manager = ProviderManager()
    assert [p.name for p in manager.get_providers_by_names(["adsb.lol"])] == ["adsb.lol"]
    assert manager.local.get_status()["configured"] is False


def test_local_receiver_joins_public_query_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_url", "http://receiver.local")
    manager = ProviderManager()
    assert [p.name for p in manager.get_providers_by_names(["adsb.lol"])] == ["local", "adsb.lol"]


def test_local_fresh_position_is_preferred_and_public_can_enrich_metadata(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_max_position_age_seconds", 12.0)
    manager = ProviderManager()
    local = _obs("local", callsign="", aircraft_type="", age=2.0)
    public = _obs("adsb.lol", callsign="THY7", aircraft_type="A359", age=0.2)
    ac = manager._merge_results({"adsb.lol": [public], "local": [local]})[0]
    assert ac.source == "local"
    assert ac.callsign == "THY7"
    assert ac.aircraft_type == "A359"
    assert ac.field_provenance["latitude"] == "local"
    assert ac.field_provenance["callsign"] == "adsb.lol"


def test_stale_local_position_yields_to_fresh_public(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_max_position_age_seconds", 12.0)
    manager = ProviderManager()
    merged = manager._merge_results({"local": [_obs("local", age=25.0)], "adsb.fi": [_obs("adsb.fi", age=1.0)]})
    assert merged[0].source == "adsb.fi"


def test_merge_order_is_deterministic():
    manager = ProviderManager()
    local = _obs("local", callsign="", age=1.0)
    public = _obs("adsb.fi", callsign="SWR123", aircraft_type="A333", age=0.5)
    left = manager._merge_results({"local": [local], "adsb.fi": [public]})[0]
    right = manager._merge_results({"adsb.fi": [public], "local": [local]})[0]
    assert left.model_dump() == right.model_dump()


def test_same_icao_from_two_providers_is_one_stable_identity():
    manager = ProviderManager()
    merged = manager._merge_results({"adsb.lol": [_obs("adsb.lol", age=1.0)], "adsb.fi": [_obs("adsb.fi", age=0.5)]})
    assert [item.icao24 for item in merged] == ["4b1801"]


def test_provider_switching_does_not_change_aircraft_identity():
    manager = ProviderManager()
    local_first = manager._merge_results({"local": [_obs("local", age=1.0)]})
    public_next = manager._merge_results({"adsb.lol": [_obs("adsb.lol", age=1.0)]})
    local_again = manager._merge_results({"local": [_obs("local", age=1.0)]})
    assert local_first[0].icao24 == public_next[0].icao24 == local_again[0].icao24


def test_conflicting_callsign_keeps_primary_source_value():
    manager = ProviderManager()
    merged = manager._merge_results({"local": [_obs("local", callsign="THY7", age=1.0)], "adsb.lol": [_obs("adsb.lol", callsign="OTHER1", age=0.2)]})
    assert merged[0].callsign == "THY7"
    assert merged[0].field_provenance["callsign"] == "local"


def test_conflicting_positions_do_not_create_synthetic_motion_state():
    manager = ProviderManager()
    merged = manager._merge_results({"local": [_obs("local", lat=41.0, lon=29.0, age=1.0)], "adsb.lol": [_obs("adsb.lol", lat=42.0, lon=30.0, age=0.2)]})
    assert merged[0].latitude == 41.0
    assert any("position" in note and "conflict" in note for note in merged[0].merge_notes)


def test_dynamic_field_is_not_borrowed_from_substantially_delayed_source():
    manager = ProviderManager()
    merged = manager._merge_results({"local": [_obs("local", age=1.0, altitude=None)], "adsb.lol": [_obs("adsb.lol", age=40.0, altitude=12000.0)]})
    assert merged[0].altitude is None


class _ProbeProvider(AircraftDataProvider):
    name = "probe"
    failure_threshold = 2

    def __init__(self):
        super().__init__()
        self.mode = "ok"
        self.request_timeout_s = 0.25

    async def get_aircraft_in_area(self, latitude, longitude, radius_nm=250):
        if self.mode == "timeout":
            await asyncio.sleep(1.0)
        if self.mode == "rate":
            raise ProviderRateLimited(30)
        if self.mode == "malformed":
            raise MalformedProviderResponse("bad payload")
        return [_obs("probe", age=0.1)]


@pytest.mark.asyncio
async def test_provider_timeout_opens_circuit_and_recovery_probe_closes_it():
    manager = ProviderManager(); probe = _ProbeProvider(); probe.mode = "timeout"
    assert await manager._safe_query(probe, 0, 0, 50) == []
    assert await manager._safe_query(probe, 0, 0, 50) == []
    assert probe.circuit_state == "open"
    assert probe.timeout_count == 2
    probe._cooldown_until_mono = 0
    assert probe.can_request_now() is True
    assert probe.circuit_state == "half_open"
    probe.mode = "ok"
    result = await manager._safe_query(probe, 0, 0, 50)
    assert result
    assert probe.circuit_state == "closed"
    assert probe.consecutive_failures == 0


@pytest.mark.asyncio
async def test_rate_limit_enters_cooldown_without_hammering():
    manager = ProviderManager(); probe = _ProbeProvider(); probe.mode = "rate"
    assert await manager._safe_query(probe, 0, 0, 50) == []
    assert probe.rate_limit_count == 1
    assert probe.circuit_state == "open"
    assert probe.cooldown_remaining_s > 0
    assert probe.can_request_now() is False


@pytest.mark.asyncio
async def test_malformed_response_is_counted_for_health():
    manager = ProviderManager(); probe = _ProbeProvider(); probe.mode = "malformed"
    assert await manager._safe_query(probe, 0, 0, 50) == []
    assert probe.malformed_response_count == 1
    assert probe.get_status()["recent_failures"] == 1


def test_provider_health_exposes_evidence_not_magic_score():
    provider = _ProbeProvider(); provider.record_success(12.0, [_obs("probe", age=1.0)])
    status = provider.get_status()
    assert status["last_latency_ms"] == 12.0
    assert status["stale_position_rate"] == 0.0
    assert status["circuit_state"] == "closed"
    assert "score" not in status and "confidence" not in status


def test_local_configuration_validation(monkeypatch):
    monkeypatch.setenv("LOCAL_ADSB_URL", "http://receiver.local")
    monkeypatch.setenv("LOCAL_ADSB_RECEIVER_TYPE", "ultrafeeder")
    configured = Settings()
    assert configured.local_adsb_url == "http://receiver.local"
    assert configured.local_adsb_receiver_type == "ultrafeeder"


def test_local_configuration_rejects_embedded_credentials(monkeypatch):
    monkeypatch.setenv("LOCAL_ADSB_URL", "http://user:pass@receiver.local")
    with pytest.raises(ValueError):
        Settings()


@pytest.mark.asyncio
async def test_local_malformed_json_and_disconnect_fall_back_without_blocking(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_url", "http://receiver.local/aircraft.json")
    manager = ProviderManager()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "receiver.local":
            return httpx.Response(200, content=b"{", request=request)
        return httpx.Response(200, json={"now": time.time(), "ac": [{"hex": "4b1801", "lat": 41.0, "lon": 29.0, "seen_pos": 0.2, "gs": 350, "track": 270}]}, request=request)

    old_client = providers_mod._http_client
    providers_mod._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        merged, by_provider = await manager.query_providers(41.0, 29.0, 100, provider_names=["adsb.lol"])
    finally:
        await providers_mod._http_client.aclose()
        providers_mod._http_client = old_client
    assert manager.local.malformed_response_count == 1
    assert "adsb.lol" in by_provider
    assert merged and merged[0].icao24 == "4b1801"


def test_unknown_freshness_is_not_promoted_to_live_merged_position():
    manager = ProviderManager()
    unknown = NormalizedAircraft(icao24="4b1801", latitude=41.0, longitude=29.0, source="local", received_at=time.time(), field_provenance={"latitude": "local", "longitude": "local"})
    assert manager._merge_results({"local": [unknown]}) == []
