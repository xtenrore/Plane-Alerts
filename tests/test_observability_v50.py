from __future__ import annotations

import json
from pathlib import Path

from app.observability_v50 import _provider_status_safe, explain_prediction
from app.version import PREDICTION_VERSION, VERSION
from app.worker import v36


def _prediction() -> dict:
    return {
        "user_id": 8083791110,
        "aircraft_icao24": "4bab24",
        "callsign": "THY5DQ",
        "aircraft_type": "A321",
        "prediction_version": "4.7.3-terminal-delivery-landing-path",
        "current_distance_km": 14.1,
        "projected_closest_km": 7.8,
        "time_to_cpa_s": 165.0,
        "state": "Approaching",
        "confidence": "Medium",
        "confidence_score": 0.61,
        "enters_alert_radius": True,
        "alert_radius_km": 10.0,
        "qualifies": False,
        "route_suppressed": True,
        "route_reason": "v4.3 CANCEL_LATCHED: confirmed cancellation protected from prediction jitter",
        "diagnostics": {
            "sample_age_s": 2.4,
            "sample_count": 8,
            "fresh_observation": True,
            "turn_rate_deg_s": -0.2,
            "route_destination": "TIA",
            "route_history_days": 3,
            "route_similar_days": 0,
            "qualification_state": "TRAJECTORY_CONFIRMING",
            "terminal_arrival_state": "TERMINAL_UNCERTAIN",
            "observation": {
                "source": "adsb.fi",
                "source_candidates": ["adsb.fi", "adsb.lol"],
                "data_quality": "good",
                "field_provenance": {"position": "adsb.fi"},
                "merge_notes": [],
            },
        },
    }


def test_v500_or_later_keeps_canonical_release_and_prediction_identifiers():
    assert tuple(int(part) for part in VERSION.split(".")) >= (5, 0, 0)
    assert isinstance(PREDICTION_VERSION, str) and PREDICTION_VERSION.strip()


def test_explanation_shows_why_alert_was_suppressed_without_private_location():
    result = explain_prediction(_prediction())
    assert result["decision"] == "route-or-terminal-suppressed"
    assert result["trajectory"]["projected_closest_km"] == 7.8
    assert result["observation"]["active_source"] == "adsb.fi"
    assert result["route_and_terminal"]["destination"] == "TIA"
    assert any("CANCEL_LATCHED" in reason for reason in result["why"])
    assert result["user_ref"].startswith("u-")

    serialized = json.dumps(result)
    assert "8083791110" not in serialized
    assert "observer_latitude" not in serialized
    assert "observer_longitude" not in serialized


def test_provider_diagnostics_allowlist_drops_endpoint_and_credentials():
    safe = _provider_status_safe(
        {
            "name": "local",
            "request_count": 12,
            "latency_p95_ms": 32.0,
            "last_success_time": 123.0,
            "stale_position_rate": 0.1,
            "circuit_state": "closed",
            "url": "http://192.0.2.50/tar1090/data/aircraft.json",
            "token": "secret",
            "api_key": "secret-key",
        }
    )
    assert safe["name"] == "local"
    assert safe["request_count"] == 12
    assert safe["circuit_state"] == "closed"
    assert "url" not in safe
    assert "token" not in safe
    assert "api_key" not in safe


def test_provider_metrics_are_sanitized_before_existing_heartbeat_write(monkeypatch):
    class FakeManager:
        def get_all_provider_status(self):
            return [
                {
                    "name": "opensky",
                    "request_count": 9,
                    "last_success_time": 123.0,
                    "circuit_state": "closed",
                    "key_rotation": {"keys": ["must-not-persist"]},
                    "token": "must-not-persist",
                }
            ]

    monkeypatch.setattr(v36.monitor, "get_provider_manager", lambda: FakeManager())
    providers, _ = v36._runtime_diagnostics_v50()
    assert providers == [
        {
            "name": "opensky",
            "request_count": 9,
            "last_success_time": 123.0,
            "circuit_state": "closed",
        }
    ]
    assert "must-not-persist" not in json.dumps(providers)


def test_planealerts_launcher_routes_v5_operator_commands():
    launcher = Path("scripts/planealerts").read_text(encoding="utf-8")
    assert '{"diagnostics", "metrics"}' in launcher
    assert "app.observability_v50" in launcher
    assert "app.doctor_v49" in launcher
