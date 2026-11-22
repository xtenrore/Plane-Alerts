from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from app.bot import next60
from app.worker import next60_diagnostics as diagnostics


@pytest.mark.asyncio
async def test_next60_diagnostics_preserve_forecast_documents(monkeypatch: pytest.MonkeyPatch, tmp_path, caplog) -> None:
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    route_db = tmp_path / "route_history.sqlite3"
    route_db.write_bytes(b"sqlite-placeholder")

    history_doc = {
        "callsign": "HIST1",
        "source": "history",
        "predicted_cpa_at": now,
        "predicted_closest_km": 4.2,
    }
    live_doc = {
        "callsign": "LIVE1",
        "source": "live",
        "predicted_cpa_at": now,
        "predicted_closest_km": 3.1,
    }

    async def fake_history(user_id: int, captured_now):
        assert user_id == 42
        assert captured_now is now
        return [history_doc]

    async def fake_live(user_id: int, captured_now):
        assert user_id == 42
        assert captured_now is now
        return [live_doc]

    async def fake_build(user_id: int, captured_now):
        history = await next60._history_docs(user_id, captured_now)
        live = await next60._live_docs(user_id, captured_now)
        return history + live

    monkeypatch.setattr(diagnostics, "_INSTALLED", False)
    monkeypatch.setattr(diagnostics, "route_db_path", lambda: route_db)
    monkeypatch.setattr(next60, "_history_docs", fake_history)
    monkeypatch.setattr(next60, "_live_docs", fake_live)
    monkeypatch.setattr(next60, "build_next60_docs", fake_build)
    monkeypatch.setattr(next60, "_volume_history_recovery_v589", True, raising=False)
    monkeypatch.delattr(next60, "_next60_diagnostics_installed", raising=False)

    caplog.set_level(logging.INFO, logger="app.worker.next60_diagnostics")
    assert diagnostics.install_next60_diagnostics() is True

    result = await next60.build_next60_docs(42, now)
    assert result == [history_doc, live_doc]
    assert "next60_history user=42 docs=1 source=volume" in caplog.text
    assert f"volume_bytes={route_db.stat().st_size}" in caplog.text
    assert "next60_live user=42 docs=1" in caplog.text
    assert "next60_build user=42 merged=2 live=1 history=1 empty=False" in caplog.text


@pytest.mark.asyncio
async def test_next60_diagnostics_do_not_swallow_source_failure(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)

    async def failing_history(user_id: int, captured_now):
        raise RuntimeError("route reader failed")

    async def fake_live(user_id: int, captured_now):
        return []

    async def fake_build(user_id: int, captured_now):
        return await next60._history_docs(user_id, captured_now)

    monkeypatch.setattr(diagnostics, "_INSTALLED", False)
    monkeypatch.setattr(diagnostics, "route_db_path", lambda: diagnostics.Path("/definitely/missing/route_history.sqlite3"))
    monkeypatch.setattr(next60, "_history_docs", failing_history)
    monkeypatch.setattr(next60, "_live_docs", fake_live)
    monkeypatch.setattr(next60, "build_next60_docs", fake_build)
    monkeypatch.delattr(next60, "_next60_diagnostics_installed", raising=False)

    caplog.set_level(logging.INFO, logger="app.worker.next60_diagnostics")
    assert diagnostics.install_next60_diagnostics() is True

    with pytest.raises(RuntimeError, match="route reader failed"):
        await next60.build_next60_docs(7, now)
    assert "next60_history_failed user=7" in caplog.text
    assert "next60_build_failed user=7" in caplog.text


def test_production_worker_installs_next60_diagnostics() -> None:
    text = diagnostics.Path("app/worker/__init__.py").read_text(encoding="utf-8")
    assert "install_next60_diagnostics" in text
    assert "install_next60_diagnostics()" in text
