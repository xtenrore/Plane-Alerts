import asyncio
from types import SimpleNamespace

import pytest

from app.intelligence import route_history_read_guard_v44 as guard
from app.intelligence.route_history import RoutePoint


@pytest.mark.asyncio
async def test_cold_route_history_read_never_blocks_live_path(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_history(_key):
        started.set()
        await release.wait()
        return [[RoutePoint(41.0, 29.0), RoutePoint(41.1, 29.1), RoutePoint(41.2, 29.2)]]

    service = SimpleNamespace(_historical_paths=slow_history)

    cold = await asyncio.wait_for(
        guard.historical_paths_nonblocking(service, "THY1017"),
        timeout=0.05,
    )
    assert cold == []
    await asyncio.wait_for(started.wait(), timeout=0.05)
    assert "THY1017" in service._route_guard_v44_history_tasks

    release.set()
    task = service._route_guard_v44_history_tasks["THY1017"]
    await asyncio.wait_for(asyncio.shield(task), timeout=0.2)

    warm = await asyncio.wait_for(
        guard.historical_paths_nonblocking(service, "THY1017"),
        timeout=0.05,
    )
    assert len(warm) == 1
    assert len(warm[0]) == 3


@pytest.mark.asyncio
async def test_stale_route_history_is_returned_while_refresh_runs(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    stale = [[RoutePoint(40.9, 28.9), RoutePoint(41.0, 29.0), RoutePoint(41.1, 29.1)]]

    async def slow_history(_key):
        started.set()
        await release.wait()
        return []

    service = SimpleNamespace(
        _historical_paths=slow_history,
        _route_guard_v2_history_cache={"THY1017": (0.0, stale)},
    )
    monkeypatch.setattr(guard.time, "monotonic", lambda: 1000.0)

    result = await asyncio.wait_for(
        guard.historical_paths_nonblocking(service, "THY1017"),
        timeout=0.05,
    )
    assert result is stale
    await asyncio.wait_for(started.wait(), timeout=0.05)

    release.set()
    await asyncio.sleep(0)
    tasks = list(service._route_guard_v44_history_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def test_route_history_read_guard_is_bounded():
    assert guard._MAX_PENDING == 16
    assert guard._READ_CONCURRENCY == 2
    assert guard._READ_TIMEOUT_S <= 1.5
    assert guard._MAX_CACHE_ENTRIES == 256
