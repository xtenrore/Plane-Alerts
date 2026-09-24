from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.intelligence import route_history as route_mod
from app.intelligence.destination_path_guard_v554 import (
    CacheEntry,
    DestinationResolution,
    DestinationResolver,
    destination_resolver,
    evaluate_destination_path,
)
from app.intelligence.trajectory import HistorySample, ProjectedPoint

LTFM = route_mod.AirportInfo(
    icao="LTFM", iata="IST", name="Istanbul Airport",
    latitude=41.2753, longitude=28.7519,
)
LTBA = route_mod.AirportInfo(
    icao="LTBA", iata="ISL", name="Istanbul Ataturk Airport",
    latitude=40.9769, longitude=28.8146,
)
ORIGIN = route_mod.AirportInfo(
    icao="LTAI", iata="AYT", name="Antalya Airport",
    latitude=36.8987, longitude=30.8005,
)


def aircraft(
    callsign="THY123",
    lat=41.10,
    lon=28.95,
    *,
    velocity=220.0,
    heading=90.0,
    altitude=3000.0,
    vertical_rate=-4.0,
):
    return SimpleNamespace(
        callsign=callsign,
        latitude=lat,
        longitude=lon,
        velocity=velocity,
        heading=heading,
        altitude=altitude,
        vertical_rate_mps=vertical_rate,
        ground_speed=velocity * 1.9438444924406,
    )


def prediction(*, current=20.0, cpa=2.0, cpa_time=120.0, entry=90.0, path=()):
    return SimpleNamespace(
        current_distance_km=current,
        projected_closest_km=cpa,
        time_to_cpa_s=cpa_time,
        radius_entry_s=entry,
        path=list(path),
        stale=False,
    )


def projected(seconds, lat, lon):
    return ProjectedPoint(
        seconds=float(seconds),
        latitude=float(lat),
        longitude=float(lon),
        horizontal_km=1.0,
        slant_km=1.0,
        altitude_m=2000.0,
        heading_deg=90.0,
    )


def history(*rows):
    return [
        HistorySample(
            timestamp=float(ts),
            latitude=float(lat),
            longitude=float(lon),
            altitude_m=float(alt),
            speed_kts=430.0,
            heading_deg=float(heading),
            vertical_rate_mps=float(vr),
        )
        for ts, lat, lon, alt, heading, vr in rows
    ]


def resolved(callsign: str, airport, source="adsb.lol+adsbdb"):
    key = route_mod.normalize_flight_key(callsign)
    route = route_mod.FlightRouteInfo(
        callsign=key,
        airport_codes=f"AYT-{airport.code}",
        plausible=True,
        origin=ORIGIN,
        destination=airport,
    )
    destination_resolver._cache[key] = CacheEntry(
        resolution=DestinationResolution(
            route=route,
            source=source,
            authority="provider-agreement" if "+" in source else "single-provider",
        ),
        state="resolved",
        expires_at=time.monotonic() + 300.0,
    )


@pytest.fixture(autouse=True)
def clean_global_resolver():
    destination_resolver.clear()
    yield
    destination_resolver.clear()


async def gate(ac, pred, samples=(), *, user_lat=41.1, user_lon=29.1, radius=10.0, sent=False):
    return await evaluate_destination_path(
        None,
        ac,
        pred,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=radius,
        current_samples=samples,
        notification_sent=sent,
    )


@pytest.mark.asyncio
async def test_ist_arrival_points_toward_user_then_turns_no_initial_alert():
    ac = aircraft("THY2GN", 41.10, 28.95, heading=90, altitude=3200, vertical_rate=-4.5)
    resolved(ac.callsign, LTFM)
    pred = prediction(
        current=18, cpa=3.5, cpa_time=130, entry=105,
        path=[
            projected(0, 41.10, 28.95),
            projected(60, 41.10, 29.08),
            projected(130, 41.10, 29.22),
        ],
    )
    result = await gate(
        ac, pred,
        history((0, 41.10, 28.93, 3400, 90, -4), (12, 41.10, 28.95, 3200, 90, -4.5)),
        user_lat=41.10, user_lon=29.22,
    )
    assert result.suppress_alert
    assert result.destination_code == "IST"
    assert "destination" in result.reason.lower()


@pytest.mark.asyncio
async def test_ltba_alignment_lands_before_user_no_initial_alert():
    ac = aircraft("MNB313", 40.90, 28.73, heading=42, altitude=1800, vertical_rate=-5)
    resolved(ac.callsign, LTBA)
    pred = prediction(
        current=23, cpa=2, cpa_time=150, entry=125,
        path=[
            projected(0, 40.90, 28.73),
            projected(55, 40.9769, 28.8146),
            projected(100, 41.02, 28.86),
            projected(150, 41.07, 28.92),
        ],
    )
    result = await gate(
        ac, pred,
        history((0, 40.885, 28.715, 2100, 42, -4.5), (12, 40.90, 28.73, 1800, 42, -5)),
        user_lat=41.07, user_lon=28.92,
    )
    assert result.suppress_alert
    assert "before" in result.reason.lower()


@pytest.mark.asyncio
async def test_destination_ist_but_user_really_lies_before_airport_alerts():
    ac = aircraft("THY321", 41.00, 28.60, heading=25, altitude=3600, vertical_rate=-2.5)
    resolved(ac.callsign, LTFM)
    pred = prediction(
        current=18, cpa=1.5, cpa_time=75, entry=55,
        path=[
            projected(0, 41.00, 28.60),
            projected(55, 41.12, 28.67),
            projected(75, 41.15, 28.69),
            projected(150, 41.25, 28.74),
        ],
    )
    result = await gate(
        ac, pred,
        history((0, 40.98, 28.59, 3800, 25, -2), (12, 41.00, 28.60, 3600, 25, -2.5)),
        user_lat=41.12, user_lon=28.67, radius=8,
    )
    assert not result.suppress_alert
    assert "genuine observer pass" in result.reason


@pytest.mark.asyncio
async def test_wrong_destination_releases_when_live_track_diverges():
    ac = aircraft("THY777", 41.00, 29.20, heading=90, altitude=4200, vertical_rate=2.5)
    resolved(ac.callsign, LTFM, source="adsbdb")
    result = await gate(
        ac,
        prediction(current=14, cpa=2, cpa_time=80, entry=55,
                   path=[projected(0, 41.00, 29.20), projected(80, 41.00, 29.38)]),
        history((0, 41.00, 29.14, 4000, 90, 2), (15, 41.00, 29.20, 4200, 90, 2.5)),
        user_lat=41.00, user_lon=29.38, radius=8,
    )
    assert not result.suppress_alert
    assert "regained authority" in result.reason


@pytest.mark.asyncio
async def test_destination_provider_unavailable_normal_live_behavior_continues():
    ac = aircraft("THY404")
    key = route_mod.normalize_flight_key(ac.callsign)
    destination_resolver._cache[key] = CacheEntry(None, "unavailable", time.monotonic() + 30)
    result = await gate(ac, prediction())
    assert not result.suppress_alert
    assert "live trajectory authoritative" in result.reason


@pytest.mark.asyncio
async def test_adsbdb_unavailable_but_adsb_lol_source_works(monkeypatch):
    resolver = DestinationResolver()
    route = route_mod.FlightRouteInfo(
        callsign="THY123", airport_codes="AYT-IST", plausible=True,
        origin=ORIGIN, destination=LTFM,
    )

    async def lol(*args):
        return route

    async def db(*args):
        return None

    monkeypatch.setattr(resolver, "_fetch_adsb_lol", lol)
    monkeypatch.setattr(resolver, "_fetch_adsbdb", db)
    await resolver._refresh("THY123", 41.0, 28.9)
    entry = resolver.cached("THY123")
    assert entry and entry.resolution and entry.resolution.source == "adsb.lol"


@pytest.mark.asyncio
async def test_all_destination_sources_unavailable_monitoring_still_works(monkeypatch):
    resolver = DestinationResolver()

    async def missing(*args):
        return None

    monkeypatch.setattr(resolver, "_fetch_adsb_lol", missing)
    monkeypatch.setattr(resolver, "_fetch_adsbdb", missing)
    await resolver._refresh("THY404", 41.0, 28.9)
    entry = resolver.cached("THY404")
    assert entry and entry.state == "unavailable" and entry.resolution is None


@pytest.mark.asyncio
async def test_slow_destination_lookup_does_not_delay_monitor_cycle(monkeypatch):
    ac = aircraft("THY888")

    async def slow_refresh(key, lat, lon):
        await asyncio.sleep(0.5)
        destination_resolver._put(key, None, "unavailable", 30)

    monkeypatch.setattr(destination_resolver, "_refresh", slow_refresh)
    started = time.perf_counter()
    result = await gate(ac, prediction())
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
    assert result.suppress_alert
    assert "held without blocking" in result.reason


@pytest.mark.asyncio
async def test_aircraft_physically_inside_radius_always_wins():
    ac = aircraft("THY999")
    resolved(ac.callsign, LTFM)
    result = await gate(ac, prediction(current=7.5), radius=10)
    assert not result.suppress_alert
    assert "physical presence" in result.reason


@pytest.mark.asyncio
async def test_go_around_or_diversion_releases_and_does_not_latch():
    ac = aircraft("THY555", 41.30, 28.70, heading=315, altitude=1900, vertical_rate=4)
    resolved(ac.callsign, LTFM)
    pred = prediction(
        current=16, cpa=2, cpa_time=90, entry=65,
        path=[projected(0, 41.30, 28.70), projected(90, 41.40, 28.58)],
    )
    samples = history(
        (0, 41.26, 28.74, 1400, 315, 3),
        (12, 41.30, 28.70, 1900, 315, 4),
    )
    first = await gate(ac, pred, samples, user_lat=41.40, user_lon=28.58)
    second = await gate(ac, pred, samples, user_lat=41.40, user_lon=28.58)
    assert not first.suppress_alert and not second.suppress_alert
    assert "regained authority" in first.reason


@pytest.mark.asyncio
async def test_provider_destination_conflict_fails_open():
    ac = aircraft("THY111")
    key = route_mod.normalize_flight_key(ac.callsign)
    destination_resolver._cache[key] = CacheEntry(None, "conflict", time.monotonic() + 60)
    result = await gate(ac, prediction())
    assert not result.suppress_alert
    assert "disagree" in result.reason


@pytest.mark.asyncio
async def test_active_alert_not_cancelled_only_because_lookup_pending(monkeypatch):
    ac = aircraft("THY222")

    async def slow_refresh(key, lat, lon):
        await asyncio.sleep(0.5)

    monkeypatch.setattr(destination_resolver, "_refresh", slow_refresh)
    result = await gate(ac, prediction(), sent=True)
    assert not result.suppress_alert


def test_production_runtime_installs_only_clean_destination_arrival_authority():
    text = Path("app/worker/__init__.py").read_text()
    assert "install_destination_path_guard()" in text
    for obsolete in (
        "install_route_guard()",
        "install_route_guard_v2()",
        "install_route_guard_v42()",
        "install_requalification_guard_v43()",
        "install_route_guard_v47()",
    ):
        assert obsolete not in text
