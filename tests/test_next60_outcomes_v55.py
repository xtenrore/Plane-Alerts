from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.next60_outcomes_v55 import resolve_next60_outcomes
from app.prediction_lab_files_v55 import write_evidence


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self, rows=None, one=None):
        self.rows = list(rows or [])
        self.one = one

    def find(self, _query, _projection=None):
        return _Cursor(self.rows)

    async def find_one(self, _query, _projection=None):
        return self.one


class _DB:
    def __init__(self, *, route):
        self.collections = {
            "users": _Collection([{"user_id": 1, "setup_complete": True}]),
            "locations": _Collection([{"user_id": 1, "latitude": 0.0, "longitude": 0.0, "radius_km": 5.0}]),
            "flight_route_samples": _Collection(one=route),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _expectation(root: Path, now: datetime) -> dict:
    predicted = now - timedelta(seconds=1300)
    doc = {
        "kind": "next60_expectation",
        "captured_at": now - timedelta(hours=1),
        "user_id": 1,
        "callsign": "TEST123",
        "aircraft_type": "A320",
        "predicted_cpa_at": predicted,
        "window_start": predicted - timedelta(seconds=300),
        "window_end": predicted + timedelta(seconds=300),
        "prediction_horizon_s": 2300.0,
        "predicted_closest_km": 3.0,
        "historical_days": 3,
        "confidence": "Low",
        "coverage_mode": "historical_flight_number_timing_shadow",
        "shadow_only": True,
    }
    write_evidence(doc, root=root)
    return doc


def _raw_docs(root: Path) -> list[dict]:
    docs = []
    for path in (root / "raw").rglob("*.json"):
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    return docs


@pytest.mark.asyncio
async def test_next60_outcome_resolver_records_observed_positive_without_private_location(tmp_path):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    expected = _expectation(tmp_path, now)
    predicted = expected["predicted_cpa_at"]
    points = [
        {"t": (predicted - timedelta(seconds=20)).timestamp(), "lat": 0.05, "lon": 0.0},
        {"t": predicted.timestamp(), "lat": 0.01, "lon": 0.0},
        {"t": (predicted + timedelta(seconds=20)).timestamp(), "lat": 0.05, "lon": 0.0},
    ]
    result = await resolve_next60_outcomes(
        _DB(route={"points": points, "aircraft_type": "A320"}),
        now=now,
        root=tmp_path,
    )
    assert result == {"examined": 1, "resolved": 1, "scoreable": 1, "inconclusive": 0}
    outcome = next(doc for doc in _raw_docs(tmp_path) if doc.get("kind") == "next60_outcome")
    assert outcome["scoreable"] is True
    assert outcome["actual_pass"] is True
    assert outcome["coverage_resolution"] == "observed_positive"
    assert outcome["shadow_only"] is True
    assert "user_id" not in outcome
    assert "latitude" not in json.dumps(outcome).lower()
    assert "longitude" not in json.dumps(outcome).lower()


@pytest.mark.asyncio
async def test_next60_missing_adsb_coverage_is_inconclusive_not_miss(tmp_path):
    now = datetime(2026, 9, 24, 12, 40, tzinfo=timezone.utc)
    predicted = now - timedelta(seconds=3600)
    write_evidence(
        {
            "kind": "next60_expectation",
            "captured_at": now - timedelta(hours=2),
            "user_id": 1,
            "callsign": "NOCOVER",
            "predicted_cpa_at": predicted,
            "window_start": predicted - timedelta(seconds=300),
            "window_end": predicted + timedelta(seconds=300),
            "prediction_horizon_s": 3300.0,
            "predicted_closest_km": 4.0,
            "confidence": "Low",
            "shadow_only": True,
        },
        root=tmp_path,
    )
    result = await resolve_next60_outcomes(_DB(route=None), now=now, root=tmp_path)
    assert result == {"examined": 1, "resolved": 1, "scoreable": 0, "inconclusive": 1}
    outcome = next(doc for doc in _raw_docs(tmp_path) if doc.get("kind") == "next60_outcome")
    assert outcome["scoreable"] is False
    assert outcome["actual_pass"] is None
    assert outcome["coverage_missing"] is True
    assert outcome["coverage_resolution"] == "inconclusive"
    assert outcome["missing_coverage_policy"] == "inconclusive"
