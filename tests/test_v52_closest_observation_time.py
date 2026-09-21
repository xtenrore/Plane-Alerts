from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import prediction_lab_audit


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
