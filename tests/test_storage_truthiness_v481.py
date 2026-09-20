"""v4.8.1 regression coverage for Motor/PyMongo database truthiness.

Real Motor/PyMongo Database objects intentionally raise NotImplementedError when
used in a boolean context. Storage helpers must select explicit database handles
with an identity check instead of ``db or fallback``.
"""
from __future__ import annotations

import pytest

from app.storage_runtime_v48 import StorageRuntimeV48


class _MotorLikeDatabase:
    def __bool__(self) -> bool:
        raise NotImplementedError("Database objects do not implement truth value testing")


@pytest.mark.asyncio
async def test_warm_accepts_motor_like_database_without_truth_testing(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = StorageRuntimeV48()
    database = _MotorLikeDatabase()

    async def load_users(db: object) -> dict[int, dict[str, object]]:
        assert db is database
        return {}

    async def load_states(db: object) -> list[dict[str, object]]:
        assert db is database
        return []

    monkeypatch.setattr(runtime, "_load_active_users", load_users)
    monkeypatch.setattr(runtime, "_load_restart_states", load_states)

    await runtime.warm(database)
    assert runtime.monitoring_safe is True


@pytest.mark.asyncio
async def test_refresh_accepts_motor_like_database_without_truth_testing(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = StorageRuntimeV48()
    database = _MotorLikeDatabase()

    async def load_users(db: object) -> dict[int, dict[str, object]]:
        assert db is database
        return {}

    monkeypatch.setattr(runtime, "_load_active_users", load_users)

    assert await runtime.refresh_config_once(database) is True
    assert runtime.monitoring_safe is True


@pytest.mark.asyncio
async def test_flush_accepts_motor_like_database_without_truth_testing(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = StorageRuntimeV48()
    database = _MotorLikeDatabase()
    runtime.enqueue_status_update("monitor_worker", {"ok": True})

    async def persist_status(db: object, key: str, version: int, values: dict[str, object]) -> None:
        assert db is database
        assert key == "monitor_worker"
        current = runtime._status_pending.get(key)
        if current is not None and current[0] == version:
            runtime._status_pending.pop(key, None)

    monkeypatch.setattr(runtime, "_persist_status", persist_status)

    assert await runtime.flush_once(database) == 1
    assert runtime.snapshot()["status_write_queue_depth"] == 0
