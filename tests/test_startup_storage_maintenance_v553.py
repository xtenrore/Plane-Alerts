from __future__ import annotations

import asyncio

import pytest

import app.database as database


async def _reset_maintenance_task() -> None:
    task = database._maintenance_task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    database._maintenance_task = None
    database._maintenance_state.update(
        {"state": "idle", "attempt": 0, "last_error": None, "completed_at": None}
    )


@pytest.mark.asyncio
async def test_railway_defaults_to_background_storage_maintenance(monkeypatch):
    await _reset_maintenance_task()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.delenv("PLANE_STORAGE_MAINTENANCE_BACKGROUND", raising=False)
    assert database._background_maintenance_enabled() is True


@pytest.mark.asyncio
async def test_railway_startup_does_not_wait_for_historical_migration(monkeypatch):
    await _reset_maintenance_task()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_INITIAL_DELAY_S", "0")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_RETRY_S", "0.1")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_MAX_RETRY_S", "0.1")

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_prepare(_db, _key):
        started.set()
        await release.wait()

    monkeypatch.setattr(database, "_prepare_connected_database", slow_prepare)

    await asyncio.wait_for(
        database._prepare_or_schedule_connected_database(object(), ("mongodb://test", "aircraft_bot")),
        timeout=0.1,
    )
    await asyncio.wait_for(started.wait(), timeout=0.5)

    snapshot = database.storage_maintenance_snapshot()
    assert snapshot["task_active"] is True
    assert snapshot["state"] == "running"

    release.set()
    assert database._maintenance_task is not None
    await asyncio.wait_for(database._maintenance_task, timeout=0.5)
    snapshot = database.storage_maintenance_snapshot()
    assert snapshot["state"] == "ready"
    assert snapshot["completed_at"]
    await _reset_maintenance_task()


@pytest.mark.asyncio
async def test_background_storage_maintenance_retries_without_killing_runtime(monkeypatch):
    await _reset_maintenance_task()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_INITIAL_DELAY_S", "0")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_RETRY_S", "0.1")
    monkeypatch.setenv("PLANE_STORAGE_MAINTENANCE_MAX_RETRY_S", "0.1")

    attempts = 0

    async def flaky_prepare(_db, _key):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic migration delay")

    monkeypatch.setattr(database, "_prepare_connected_database", flaky_prepare)
    database._schedule_connected_database_maintenance(object(), ("mongodb://test", "aircraft_bot"))

    assert database._maintenance_task is not None
    await asyncio.wait_for(database._maintenance_task, timeout=1.0)
    snapshot = database.storage_maintenance_snapshot()
    assert attempts == 2
    assert snapshot["state"] == "ready"
    assert snapshot["attempt"] == 2
    assert snapshot["last_error"] is None
    await _reset_maintenance_task()


@pytest.mark.asyncio
async def test_non_railway_keeps_synchronous_release_gate_by_default(monkeypatch):
    await _reset_maintenance_task()
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("PLANE_STORAGE_MAINTENANCE_BACKGROUND", raising=False)

    calls = []

    async def prepare(db, key):
        calls.append((db, key))

    marker = object()
    key = ("mongodb://test", "aircraft_bot")
    monkeypatch.setattr(database, "_prepare_connected_database", prepare)
    await database._prepare_or_schedule_connected_database(marker, key)

    assert calls == [(marker, key)]
    assert database._maintenance_task is None
