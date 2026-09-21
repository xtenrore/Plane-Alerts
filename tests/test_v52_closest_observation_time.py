from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import prediction_lab_audit
from app.shadow_evaluation_v52 import evaluate_snapshot_outcome


def _aircraft():
    return SimpleNamespace(icao24="abc123", callsign="TEST1", aircraft_type="A320")


def test_closest_observation_tracker_preserves_time_of_minimum(monkeypatch):
    prediction_lab_audit._closest_observation.clear()
    monkeypatch.setattr(prediction_lab_audit.time, "monotonic", lambda: 5000.0)

    prediction_lab_audit._track_closest_observation(
        7,
        "abc123",
        8.0,
        {"last_observation_at": 1_700_000_000.0},
    )
    prediction_lab_audit._track_closest_observation(
        7,
        "abc123",
        5.5,
        {"last_observation_at": 1_700_000_005.0},
    )
    prediction_lab_audit._track_closest_observation(
        7,
        "abc123",
        6.2,
        {"last_observation_at": 1_700_000_010.0},
    )

    distance, observed_at, _ = prediction_lab_audit._closest_observation[(7, "abc123")]
    assert distance == 5.5
    assert observed_at == datetime.fromtimestamp(1_700_000_005.0, tz=timezone.utc)


def test_enqueue_outcome_attaches_matching_closest_timestamp(monkeypatch):
    prediction_lab_audit._closest_observation.clear()
    expected = datetime.fromtimestamp(1_700_000_005.0, tz=timezone.utc)
    prediction_lab_audit._closest_observation[(7, "abc123")] = (5.5, expected, 5000.0)
    captured = {}

    def fake_record_prediction_outcome(**kwargs):
        captured.update(kwargs)
        return None

    class FakeWork:
        def get(self, _key, factory, *, ttl):
            assert ttl == 30
            return factory()

    monkeypatch.setattr(prediction_lab_audit, "record_prediction_outcome", fake_record_prediction_outcome)
    monkeypatch.setattr(prediction_lab_audit, "audit_work", FakeWork())

    prediction_lab_audit.enqueue_outcome(
        user_id=7,
        aircraft=_aircraft(),
        outcome="passed",
        observed_closest_km=5.52,
        final_prediction=SimpleNamespace(),
    )

    assert captured["observed_closest_at"] == expected
    assert (7, "abc123") not in prediction_lab_audit._closest_observation


def test_enqueue_outcome_does_not_claim_timestamp_for_mismatched_distance(monkeypatch):
    prediction_lab_audit._closest_observation.clear()
    prediction_lab_audit._closest_observation[(7, "abc123")] = (
        3.0,
        datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc),
        5000.0,
    )
    captured = {}

    def fake_record_prediction_outcome(**kwargs):
        captured.update(kwargs)
        return None

    class FakeWork:
        def get(self, _key, factory, *, ttl):
            return factory()

    monkeypatch.setattr(prediction_lab_audit, "record_prediction_outcome", fake_record_prediction_outcome)
    monkeypatch.setattr(prediction_lab_audit, "audit_work", FakeWork())

    prediction_lab_audit.enqueue_outcome(
        user_id=7,
        aircraft=_aircraft(),
        outcome="passed",
        observed_closest_km=8.0,
        final_prediction=SimpleNamespace(),
    )

    assert "observed_closest_at" not in captured


def test_missing_closest_timestamp_keeps_eta_unscored():
    snapshot = {
        "_id": "snap",
        "release_version": "5.2.0",
        "prediction_version": "5.1-3d-proximity",
        "captured_at": "2026-09-21T05:00:00+00:00",
        "aircraft_icao24": "abc123",
        "user_id": 7,
        "alert_radius_km": 9.0,
        "projected_closest_km": 5.0,
        "time_to_cpa_s": 120.0,
        "enters_alert_radius": True,
        "confidence_score": 0.8,
        "diagnostics": {},
    }
    outcome = {
        "_id": "out",
        "outcome": "passed",
        "outcome_basis": "observed_in_radius_pass",
        "scoreable": True,
        "captured_at": "2026-09-21T05:04:00+00:00",
        "observed_closest_km": 4.5,
    }

    rows = evaluate_snapshot_outcome(snapshot, outcome)
    assert len(rows) == 1
    assert rows[0]["cpa_error_km"] == 0.5
    assert rows[0]["actual_eta_s"] is None
    assert rows[0]["eta_error_s"] is None
    assert rows[0]["actual_eta_basis"] == "unavailable"
