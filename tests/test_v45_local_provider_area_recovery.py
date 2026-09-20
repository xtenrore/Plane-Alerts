"""Integration regressions for v4.5 local ADS-B scoping and recovery."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import app.aircraft.providers as providers_mod
from app.aircraft.providers import ProviderManager
from app.config import settings


def _public_payload(*, icao: str = "4b1801", lat: float = 41.0, lon: float = 29.0) -> dict:
    return {
        "now": time.time(),
        "ac": [
            {
                "hex": icao,
                "flight": "THY7 ",
                "lat": lat,
                "lon": lon,
                "seen_pos": 0.2,
                "gs": 350,
                "track": 270,
                "t": "A359",
            }
        ],
    }


def _local_payload() -> dict:
    return {
        "now": time.time(),
        "aircraft": [
            {
                "hex": "4b1801",
                "flight": "THY7 ",
                "lat": 41.01,
                "lon": 29.01,
                "seen_pos": 0.1,
                "gs": 352,
                "track": 271,
                "t": "A359",
            },
            {
                "hex": "400001",
                "flight": "REMOTE1 ",
                "lat": 51.50,
                "lon": -0.12,
                "seen_pos": 0.1,
                "gs": 410,
                "track": 90,
                "t": "B738",
            },
            {
                "hex": "nopos1",
                "flight": "NOPOS ",
                "seen": 0.1,
            },
        ],
    }


@pytest.mark.asyncio
async def test_local_receiver_is_spatially_scoped_to_requested_provider_region(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_url", "http://receiver.local/aircraft.json")
    manager = ProviderManager()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_local_payload(), request=request)

    old_client = providers_mod._http_client
    providers_mod._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        istanbul = await manager._safe_query(manager.local, 41.0, 29.0, 60)
        manager.local._last_request_mono = 0.0
        london = await manager._safe_query(manager.local, 51.50, -0.12, 60)
        manager.local._last_request_mono = 0.0
        remote_miss = await manager._safe_query(manager.local, 48.85, 2.35, 60)
    finally:
        await providers_mod._http_client.aclose()
        providers_mod._http_client = old_client

    assert [ac.icao24 for ac in istanbul] == ["4b1801"]
    assert [ac.icao24 for ac in london] == ["400001"]
    assert remote_miss == []
    assert all(ac.has_position for ac in istanbul + london)


@pytest.mark.asyncio
async def test_local_disconnect_cooldown_public_fallback_and_recovery(monkeypatch):
    monkeypatch.setattr(settings, "local_adsb_url", "http://receiver.local/aircraft.json")
    monkeypatch.setattr(settings, "local_adsb_timeout_seconds", 0.2)
    manager = ProviderManager()
    state = {"local_mode": "timeout", "local_requests": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "receiver.local":
            state["local_requests"] += 1
            if state["local_mode"] == "timeout":
                await asyncio.sleep(0.5)
            return httpx.Response(200, json=_local_payload(), request=request)
        return httpx.Response(200, json=_public_payload(), request=request)

    old_client = providers_mod._http_client
    providers_mod._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        first, first_by_provider = await manager.query_providers(41.0, 29.0, 60, provider_names=["adsb.lol"])
        manager.local._last_request_mono = 0.0
        second, second_by_provider = await manager.query_providers(41.0, 29.0, 60, provider_names=["adsb.lol"])

        assert first and second
        assert first[0].source == "adsb.lol"
        assert second[0].source == "adsb.lol"
        assert "adsb.lol" in first_by_provider and "adsb.lol" in second_by_provider
        assert manager.local.timeout_count == 2
        assert manager.local.circuit_state == "open"

        requests_before_cooldown_query = state["local_requests"]
        manager.adsb_lol._last_request_mono = 0.0
        during_cooldown, during_by_provider = await manager.query_providers(41.0, 29.0, 60, provider_names=["adsb.lol"])
        assert during_cooldown and during_cooldown[0].source == "adsb.lol"
        assert "local" not in during_by_provider
        assert state["local_requests"] == requests_before_cooldown_query

        state["local_mode"] = "healthy"
        manager.local._cooldown_until_mono = 0.0
        manager.local._last_request_mono = 0.0
        manager.adsb_lol._last_request_mono = 0.0
        recovered, recovered_by_provider = await manager.query_providers(41.0, 29.0, 60, provider_names=["adsb.lol"])
    finally:
        await providers_mod._http_client.aclose()
        providers_mod._http_client = old_client

    assert recovered
    assert recovered[0].icao24 == "4b1801"
    assert recovered[0].source == "local"
    assert set(recovered_by_provider) == {"local", "adsb.lol"}
    assert manager.local.circuit_state == "closed"
    assert manager.local.consecutive_failures == 0


def test_temporary_source_disappearance_does_not_duplicate_aircraft_identity():
    manager = ProviderManager()
    now = time.time()

    def obs(source: str, age: float):
        from app.aircraft.models import NormalizedAircraft

        return NormalizedAircraft(
            icao24="4b1801",
            latitude=41.0,
            longitude=29.0,
            velocity=180.0,
            heading=270.0,
            position_age_s=age,
            observed_at=now - age,
            received_at=now,
            timestamp=now - age,
            source=source,
            field_provenance={"latitude": source, "longitude": source},
            source_candidates=[source],
        )

    both = manager._merge_results({"local": [obs("local", 1.0)], "adsb.lol": [obs("adsb.lol", 0.5)]})
    public_only = manager._merge_results({"adsb.lol": [obs("adsb.lol", 0.5)]})
    local_returns = manager._merge_results({"local": [obs("local", 1.0)], "adsb.lol": [obs("adsb.lol", 0.5)]})

    assert len(both) == len(public_only) == len(local_returns) == 1
    assert both[0].icao24 == public_only[0].icao24 == local_returns[0].icao24 == "4b1801"
