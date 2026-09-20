import asyncio
from types import SimpleNamespace

import pytest

import app.intelligence.route_guard_v2 as guard
from app.intelligence.route_history import AirportInfo, FlightRouteInfo, RouteGateResult, RouteHistoryService


@pytest.mark.asyncio
async def test_route_evaluation_never_waits_for_network_refresh(monkeypatch):
    service = RouteHistoryService()

    async def no_history(key):
        return []

    scheduled = []
    service._historical_paths = no_history
    monkeypatch.setattr(guard, "_schedule_route_refresh", lambda _service, ac: scheduled.append(ac.callsign))

    ac = SimpleNamespace(
        callsign="THY1017",
        latitude=41.10,
        longitude=29.15,
        altitude=5000.0,
        vertical_rate_mps=-4.0,
        ground_speed=280.0,
        heading=260.0,
    )
    pred = SimpleNamespace(
        stale=False,
        current_distance_km=25.0,
        time_to_cpa_s=130.0,
        path=[],
    )
    result = await guard.evaluate_route_nonblocking(
        service,
        ac,
        pred,
        user_lat=41.0,
        user_lon=29.0,
        alert_radius_km=15.0,
        current_samples=[(41.10, 29.15), (41.09, 29.13), (41.08, 29.11)],
    )

    assert scheduled == ["THY1017"]
    assert result.callsign == "THY1017"
    assert result.suppress_alert is True
    assert "pending in background" in result.reason


@pytest.mark.asyncio
async def test_route_history_observation_returns_without_waiting_for_database(monkeypatch):
    service = RouteHistoryService()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_observe(self, ac, *, now=None):
        started.set()
        await release.wait()

    monkeypatch.setattr(guard, "_ORIGINAL_OBSERVE", slow_observe)
    ac = SimpleNamespace(callsign="THY1017")

    await asyncio.wait_for(guard.observe_nonblocking(service, ac, now=100.0), timeout=0.05)
    await asyncio.wait_for(started.wait(), timeout=0.05)
    assert "THY1017" in service._route_guard_v2_observe_tasks

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_history_reads_are_cached_for_live_candidate(monkeypatch):
    service = RouteHistoryService()
    calls = 0

    async def history(key):
        nonlocal calls
        calls += 1
        return []

    service._historical_paths = history

    # A cold read must return immediately from the live alert path and schedule
    # exactly one background refresh. A second hot-path lookup while that refresh
    # is pending must share the same work rather than spawning another DB read.
    assert await guard._historical_paths_cached(service, "THY1017") == []
    assert calls == 0
    task = service._route_guard_v2_history_tasks["THY1017"]
    assert await guard._historical_paths_cached(service, "THY1017") == []
    assert service._route_guard_v2_history_tasks["THY1017"] is task
    assert calls == 0

    await asyncio.wait_for(task, timeout=0.1)
    assert calls == 1

    # The completed refresh is reused from memory without another history read.
    assert await guard._historical_paths_cached(service, "THY1017") == []
    assert calls == 1


def test_route_pending_grace_is_bounded(monkeypatch):
    service = RouteHistoryService()
    result = RouteGateResult(False, "THY1017", "no route")
    times = iter([100.0, 103.0, 107.0])
    monkeypatch.setattr(guard.time, "monotonic", lambda: next(times))

    first = guard._bounded_pending_gate(service, "THY1017", result, directly_inside=False)
    second = guard._bounded_pending_gate(service, "THY1017", result, directly_inside=False)
    third = guard._bounded_pending_gate(service, "THY1017", result, directly_inside=False)

    assert first.suppress_alert is True
    assert second.suppress_alert is True
    assert third.suppress_alert is False


def test_route_pending_grace_never_hides_direct_observed_presence():
    service = RouteHistoryService()
    result = RouteGateResult(False, "THY1017", "no route")
    direct = guard._bounded_pending_gate(service, "THY1017", result, directly_inside=True)
    assert direct.suppress_alert is False


def test_conflicting_sources_keep_position_aware_primary_destination():
    ist = AirportInfo(icao="LTFM", iata="IST", latitude=41.2753, longitude=28.7519)
    saw = AirportInfo(icao="LTFJ", iata="SAW", latitude=40.8986, longitude=29.3092)
    primary = FlightRouteInfo("THY1017", "AAA-IST", True, destination=ist)
    fallback = FlightRouteInfo("THY1017", "AAA-SAW", True, destination=saw)

    route, source = guard._choose_route_prefer_position_aware(
        "THY1017",
        [("adsb.im", primary), ("adsbdb", fallback)],
    )

    assert route is primary
    assert route.destination is ist
    assert source == "adsb.im:conflict"
