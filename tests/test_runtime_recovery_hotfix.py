from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.worker import runtime_recovery_hotfix as recovery


def _aircraft(icao: str, source: str):
    return SimpleNamespace(
        icao24=icao,
        source=source,
        field_provenance={"latitude": source, "longitude": source},
    )


def test_public_provider_switch_requires_three_consecutive_observations():
    manager = SimpleNamespace()
    first = _aircraft("4baa53", "adsb.fi")
    assert recovery._guard_source_switches(manager, [first], now=100.0) == [first]

    switch1 = _aircraft("4baa53", "adsb.lol")
    switch2 = _aircraft("4baa53", "adsb.lol")
    switch3 = _aircraft("4baa53", "adsb.lol")
    assert recovery._guard_source_switches(manager, [switch1], now=105.0) == []
    assert recovery._guard_source_switches(manager, [switch2], now=110.0) == []
    assert recovery._guard_source_switches(manager, [switch3], now=115.0) == [switch3]


def test_oscillation_does_not_confirm_a_replacement_source():
    manager = SimpleNamespace()
    original = _aircraft("4cac15", "adsb.fi")
    assert recovery._guard_source_switches(manager, [original], now=100.0) == [original]

    for index, source in enumerate(("adsb.lol", "adsb.fi", "adsb.lol", "adsb.fi"), start=1):
        ac = _aircraft("4cac15", source)
        result = recovery._guard_source_switches(manager, [ac], now=100.0 + index * 5.0)
        assert result == ([ac] if source == "adsb.fi" else [])


def test_local_receiver_handoff_is_immediate():
    manager = SimpleNamespace()
    public = _aircraft("4baa92", "adsb.fi")
    local = _aircraft("4baa92", "local")
    assert recovery._guard_source_switches(manager, [public], now=100.0) == [public]
    assert recovery._guard_source_switches(manager, [local], now=105.0) == [local]


def test_next60_history_reader_uses_authoritative_volume_sqlite(tmp_path: Path):
    path = tmp_path / "route_history.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE route_days (
            callsign TEXT NOT NULL,
            utc_date TEXT NOT NULL,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            aircraft_type TEXT,
            points_json TEXT NOT NULL,
            PRIMARY KEY (callsign, utc_date)
        )
        """
    )
    points = [{"t": 1_790_000_000.0, "lat": 41.0, "lon": 29.0}]
    conn.execute(
        "INSERT INTO route_days VALUES (?, ?, ?, ?, ?, ?)",
        ("THY1VP", "2026-09-26", 1.0, 2.0, "B789", json.dumps(points)),
    )
    conn.commit()
    conn.close()

    docs = recovery._read_volume_routes(path, ["2026-09-26"], 30)
    assert docs == [
        {
            "callsign": "THY1VP",
            "utc_date": "2026-09-26",
            "aircraft_type": "B789",
            "points": points,
        }
    ]


def test_recovery_only_replaces_next60_history_not_pr131_live_logic():
    source = inspect.getsource(recovery._install_next60_volume_history)
    assert "_history_docs" in source
    assert "_live_docs" not in source


def test_worker_installs_recovery_before_monitor_timing_wrappers():
    import app.worker

    source = inspect.getsource(app.worker)
    assert "install_runtime_recovery_hotfix" in source
    assert source.index("install_runtime_recovery_hotfix") < source.index("install_critical_timing_guards")
