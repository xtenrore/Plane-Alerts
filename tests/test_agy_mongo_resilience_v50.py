from __future__ import annotations

import json
import logging
from pathlib import Path

from pymongo.errors import NetworkTimeout

from app import agy_prediction_bridge as bridge


class _TimeoutCursor:
    def sort(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def max_time_ms(self, *args, **kwargs):
        return self

    def __iter__(self):
        raise NetworkTimeout("simulated Atlas read timeout")


class _TimeoutCollection:
    def find(self, *args, **kwargs):
        return _TimeoutCursor()


class _TimeoutDB:
    def __getitem__(self, name):
        return _TimeoutCollection()


def test_timeout_opens_circuit_and_returns_last_known_good_without_traceback(monkeypatch, caplog):
    bridge._reset_resilience_state_for_tests()
    bridge._recent_cache["prediction_lab_audit"] = [{"callsign": "CACHED", "state": "Approaching"}]
    monkeypatch.setattr(bridge, "_db", lambda: _TimeoutDB())

    with caplog.at_level(logging.WARNING):
        rows = bridge._recent("prediction_lab_audit", "captured_at", 10)

    assert rows == [{"callsign": "CACHED", "state": "Approaching"}]
    status = bridge.bridge_status()
    assert status["mongo_failures"] == 1
    assert status["mongo_retry_in_s"] > 0
    assert "AGY_MONGO_DEGRADED" in caplog.text
    assert "Traceback" not in caplog.text


def test_persisted_redacted_context_survives_mongo_unavailability(tmp_path: Path, monkeypatch):
    context = tmp_path / "latest.json"
    context.write_text(
        json.dumps(
            {
                "approach_states": [{"callsign": "THY5DQ", "stage": "cancelled"}],
                "prediction_audit": [{"callsign": "THY5DQ", "horizon_bucket": "0-10m"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(bridge, "CONTEXT_FILE", context)
    monkeypatch.setattr(bridge, "_db", lambda: None)
    bridge._reset_resilience_state_for_tests()

    payload = bridge.build_context_snapshot()

    assert payload["summary"]["approach_state_records"] == 1
    assert payload["summary"]["cancelled_alerts"] == 1
    assert payload["summary"]["prediction_audit_records"] == 1
    assert payload["approach_states"][0]["callsign"] == "THY5DQ"
    assert payload["prediction_audit"][0]["callsign"] == "THY5DQ"


def test_default_agy_mongo_timeouts_are_bounded():
    assert 250 <= bridge._MONGO_TIMEOUT_MS <= 2500
    assert 100 <= bridge._MONGO_QUERY_MAX_MS <= bridge._MONGO_TIMEOUT_MS
    assert bridge._MONGO_TIMEOUT_MS < 8000
