from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.bot import next60
from app.bot.replay_history import (
    build_history_doc,
    circular_time_center,
    history_day_keys,
    parse_point_timestamp,
)
from app.intelligence.route_guard import _result
from app.worker import runtime_recovery_hotfix as recovery


def _point(timestamp, lat: float, lon: float) -> dict:
    return {"t": timestamp, "lat": lat, "lon": lon}


def _transit(timestamp, *, lat: float = 41.0, lon: float = 29.0) -> list[dict]:
    return [
        _point(timestamp - 120, lat, lon - 0.20),
        _point(timestamp, lat, lon),
        _point(timestamp + 120, lat, lon + 0.20),
    ]


def test_replay_timestamp_parser_accepts_datetime_milliseconds_and_iso():
    expected = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
    assert parse_point_timestamp(expected) == expected.timestamp()
    assert parse_point_timestamp(expected.timestamp() * 1000.0) == expected.timestamp()
    assert parse_point_timestamp("2026-09-26T10:00:00Z") == expected.timestamp()


def test_replay_time_center_is_safe_across_midnight():
    center, spread = circular_time_center([23 * 3600 + 58 * 60, 2 * 60])
    assert center <= 60.0 or center >= 86340.0
    assert spread <= 120.1


def test_replay_days_include_today_and_previous_window():
    now = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
    days = history_day_keys(now, 7)
    assert days[0] == "2026-09-27"
    assert days[-1] == "2026-09-20"
    assert len(days) == 8


def test_history_shadow_requires_two_demonstrated_distinct_days():
    now = datetime(2026, 9, 27, 9, 45, tzinfo=timezone.utc)
    first = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
    second = datetime(2026, 9, 26, 10, 2, tzinfo=timezone.utc)
    routes = [
        {
            "callsign": "THY1VP",
            "utc_date": "2026-09-25",
            "aircraft_type": "B789",
            "points": _transit(first.timestamp()),
        },
        {
            "callsign": "THY1VP",
            "utc_date": datetime(2026, 9, 26, tzinfo=timezone.utc),
            "aircraft_type": "B789",
            "points": _transit(second.timestamp() * 1000.0),
        },
    ]
    doc = build_history_doc("THY1VP", routes, 41.0, 29.0, 15.0, now)
    assert doc is not None
    assert doc["historical_days"] == 2
    assert doc["confidence"] == "Medium"

    assert build_history_doc("THY1VP", routes[:1], 41.0, 29.0, 15.0, now) is None


def test_incomplete_route_ending_at_closest_point_is_inconclusive():
    now = datetime(2026, 9, 27, 9, 45, tzinfo=timezone.utc)
    timestamp = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc).timestamp()
    incomplete = {
        "callsign": "THY2GN",
        "utc_date": "2026-09-26",
        "points": [
            _point(timestamp - 120, 41.0, 28.80),
            _point(timestamp, 41.0, 29.0),
        ],
    }
    assert build_history_doc("THY2GN", [incomplete], 41.0, 29.0, 15.0, now) is None


def test_low_confidence_30_to_60_min_history_shadow_is_not_surfaced():
    now = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)
    routes = []
    for day in (24, 25, 26):
        timestamp = datetime(2026, 9, day, 9, 45, tzinfo=timezone.utc).timestamp()
        routes.append(
            {
                "callsign": "THY9AB",
                "utc_date": f"2026-09-{day:02d}",
                "points": _transit(timestamp),
            }
        )
    assert build_history_doc("THY9AB", routes, 41.0, 29.0, 15.0, now) is None


def test_midnight_shadow_rolls_to_next_utc_day_when_within_next_hour():
    now = datetime(2026, 9, 27, 23, 50, tzinfo=timezone.utc)
    routes = [
        {
            "callsign": "THY7MD",
            "utc_date": "2026-09-25",
            "points": _transit(datetime(2026, 9, 25, 23, 58, tzinfo=timezone.utc).timestamp()),
        },
        {
            "callsign": "THY7MD",
            "utc_date": "2026-09-26",
            "points": _transit(datetime(2026, 9, 26, 0, 2, tzinfo=timezone.utc).timestamp()),
        },
    ]
    doc = build_history_doc("THY7MD", routes, 41.0, 29.0, 15.0, now)
    assert doc is not None
    assert doc["predicted_cpa_at"].date().isoformat() == "2026-09-28"
    assert 0 <= doc["prediction_horizon_s"] <= 900


def test_volume_route_reader_geographically_bounds_candidates(tmp_path: Path):
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
    near = _transit(1_790_000_000.0)
    far = _transit(1_790_000_000.0, lat=35.0, lon=35.0)
    conn.execute(
        "INSERT INTO route_days VALUES (?, ?, ?, ?, ?, ?)",
        ("NEAR1", "2026-09-26", 2.0, 3.0, "A320", json.dumps(near)),
    )
    conn.execute(
        "INSERT INTO route_days VALUES (?, ?, ?, ?, ?, ?)",
        ("FAR1", "2026-09-26", 1.0, 3.0, "B738", json.dumps(far)),
    )
    conn.commit()
    conn.close()

    docs = recovery._read_volume_routes(
        path,
        ["2026-09-26"],
        30,
        observer_lat=41.0,
        observer_lon=29.0,
        corridor_km=55.0,
    )
    assert [doc["callsign"] for doc in docs] == ["NEAR1"]


def test_canonical_route_result_persists_exact_suppression_flag():
    assert _result("THY1", True, "terminal").expected_turn_pending is True
    assert _result("THY1", False, "live trajectory").expected_turn_pending is False


class _AsyncCursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def limit(self, _limit: int):
        return self

    def __aiter__(self):
        self._iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def find(self, *_args, **_kwargs):
        return _AsyncCursor(self.rows)


class _DB:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def __getitem__(self, name: str):
        assert name == "approach_states"
        return _Collection(self.rows)


@pytest.mark.asyncio
async def test_live_next60_hides_exact_current_route_veto(monkeypatch):
    now = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    base = {
        "aircraft_type": "A320",
        "projected_closest_km": 4.2,
        "time_to_cpa_s": 300.0,
        "prediction_at": now,
        "confidence": "High",
        "stage": "prepare",
    }
    rows = [
        {
            **base,
            "aircraft_icao24": "4baa01",
            "route_callsign": "THYARR",
            "route_expected_turn_pending": True,
        },
        {
            **base,
            "aircraft_icao24": "4baa02",
            "route_callsign": "THYPASS",
            "route_expected_turn_pending": False,
        },
    ]
    monkeypatch.setattr(next60, "get_db", lambda: _DB(rows))
    docs = await recovery._live_docs_route_filtered(1, now)
    assert [doc["callsign"] for doc in docs] == ["THYPASS"]
