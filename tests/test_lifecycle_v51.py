from __future__ import annotations

from types import SimpleNamespace

from app.intelligence.lifecycle import should_finalize_observed_pass
from app.intelligence.trajectory import HistorySample, predict_trajectory
from app.version import PREDICTION_VERSION, VERSION
from app.worker import monitor

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def _prediction(**overrides):
    values = {
        "stale": False,
        "current_distance_km": 7.1,
        "distance_trend_km_s": 0.03,
        "state": "Moving away",
        "already_passed": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sample(lat, *, heading=0.0, t=NOW, alt=1200.0):
    return HistorySample(t, lat, 29.0, alt, 360.0, heading, 0.0, 0.0)


def test_v510_has_explicit_physical_prediction_identity():
    assert VERSION == "5.1.0"
    assert PREDICTION_VERSION == "5.1-3d-proximity"


def test_observed_radius_entry_that_is_now_receding_finishes_as_passed():
    pred = _prediction(current_distance_km=7.1, distance_trend_km_s=0.025)
    assert should_finalize_observed_pass(pred, observed_closest_km=6.37, alert_radius_km=15.0)


def test_projected_close_cpa_is_not_enough_to_claim_an_observed_pass():
    pred = _prediction(current_distance_km=19.0, distance_trend_km_s=0.04, already_passed=True)
    # Mirrors the AGY failure mode: a ~6 km projected CPA must not be confused
    # with ground truth if the closest actually observed position stayed outside.
    assert not should_finalize_observed_pass(pred, observed_closest_km=18.8, alert_radius_km=15.0)


def test_stale_data_never_finalizes_an_observed_pass():
    pred = _prediction(stale=True, current_distance_km=7.5)
    assert not should_finalize_observed_pass(pred, observed_closest_km=6.0, alert_radius_km=15.0)


def test_predictor_does_not_call_a_turnaway_outside_radius_a_pass():
    hist = [
        _sample(41.10, t=NOW - 10),
        _sample(41.11, t=NOW - 5),
        _sample(41.12, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert not pred.already_passed
    assert pred.state != "Passed"


def test_predictor_marks_real_observed_radius_entry_as_passed_once_receding():
    hist = [
        _sample(41.012, t=NOW - 10),
        _sample(41.020, t=NOW - 5),
        _sample(41.030, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 2.0, now=NOW, user_altitude_m=100.0)
    assert pred.already_passed
    assert pred.state == "Passed"


def test_altitude_relevance_setting_is_configurable_and_defaults_safe_on():
    assert monitor._altitude_relevance_enabled({}) is True
    assert monitor._altitude_relevance_enabled({"proximity_3d": {"altitude_relevance": True}}) is True
    assert monitor._altitude_relevance_enabled({"proximity_3d": {"altitude_relevance": False}}) is False


def test_missing_observer_elevation_is_queued_as_optional_work(monkeypatch):
    calls = []

    def fake_get(key, factory, *, default=None, ttl=120.0):
        calls.append((key, factory, default, ttl))
        return default

    monkeypatch.setattr(monitor.enrichment, "get", fake_get)
    monitor._queue_observer_elevation(42, {"latitude": 41.1234, "longitude": 29.1234})
    assert len(calls) == 1
    key, factory, default, ttl = calls[0]
    assert key[:2] == ("observer_elevation_v51", 42)
    assert callable(factory)
    assert default is None
    assert ttl == 900


def test_existing_observer_elevation_skips_optional_lookup(monkeypatch):
    called = False

    def fake_get(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(monitor.enrichment, "get", fake_get)
    monitor._queue_observer_elevation(
        42,
        {"latitude": 41.1234, "longitude": 29.1234, "elevation_m": 88.0},
    )
    assert called is False
