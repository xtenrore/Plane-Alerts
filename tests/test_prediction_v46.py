from types import SimpleNamespace

import pytest

from app.intelligence.trajectory import HistorySample, haversine_km
from app.intelligence.prediction_v46 import (
    diagnostics_for,
    freshness_limit_s,
    position_uncertainty_km,
    predict_trajectory_v46,
    reset_v46_state_for_tests,
    turn_evidence,
)

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def sample(lat, lon, heading, *, speed=420.0, t=NOW, age=0.0, alt=3500.0, vr=0.0):
    return HistorySample(t, lat, lon, alt, speed, heading, vr, age)


@pytest.fixture(autouse=True)
def _reset():
    reset_v46_state_for_tests()
    yield
    reset_v46_state_for_tests()


def test_speed_dependent_freshness_is_bounded_and_faster_tightens_limit():
    slow = freshness_limit_s(100)
    airliner = freshness_limit_s(450)
    fast = freshness_limit_s(900)
    assert 8.0 <= fast < airliner < slow <= 30.0


def test_high_speed_stale_aircraft_rejected_before_slower_aircraft_same_age():
    high = [
        sample(41.30, 29.0, 180, speed=900, t=NOW - 30),
        sample(41.27, 29.0, 180, speed=900, t=NOW - 25),
        sample(41.24, 29.0, 180, speed=900, t=NOW - 20),
        sample(41.21, 29.0, 180, speed=900, t=NOW - 15, age=15),
    ]
    slow = [
        sample(41.10, 29.0, 180, speed=100, t=NOW - 30),
        sample(41.09, 29.0, 180, speed=100, t=NOW - 25),
        sample(41.08, 29.0, 180, speed=100, t=NOW - 20),
        sample(41.07, 29.0, 180, speed=100, t=NOW - 15, age=15),
    ]
    p_fast = predict_trajectory_v46(high, *USER, 15, now=NOW)
    p_slow = predict_trajectory_v46(slow, *USER, 15, now=NOW)
    assert p_fast.stale
    assert p_fast.state == "Prediction uncertain"
    assert not p_fast.enters_alert_radius
    assert not p_slow.stale


def test_position_uncertainty_grows_faster_for_fast_aircraft():
    slow = [
        sample(41.1, 29.0, 180, speed=100, t=NOW - 5, age=5),
        sample(41.09, 29.0, 180, speed=100, t=NOW, age=5),
    ]
    fast = [
        sample(41.1, 29.0, 180, speed=800, t=NOW - 5, age=5),
        sample(41.06, 29.0, 180, speed=800, t=NOW, age=5),
    ]
    assert position_uncertainty_km(fast, now=NOW + 5) > position_uncertainty_km(slow, now=NOW + 5)


def test_stable_straight_flight_keeps_turn_candidate_linear():
    hist = [
        sample(41.30, 29.0, 180, t=NOW - 20),
        sample(41.28, 29.0, 180, t=NOW - 15),
        sample(41.26, 29.0, 180, t=NOW - 10),
        sample(41.24, 29.0, 180, t=NOW - 5),
        sample(41.22, 29.0, 180, t=NOW),
    ]
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    diag = diagnostics_for(pred)
    assert pred.confidence in {"High", "Medium", "Low", "Uncertain"}
    assert diag["shadow_only"] is True
    assert diag["turn_evidence"]["stable"] is False
    assert diag["turn_shadow"]["used_turn"] is False
    assert diag["turn_shadow"]["cpa_km"] == pytest.approx(diag["linear_shadow"]["cpa_km"], abs=1e-9)


def test_stable_turn_requires_sustained_consistent_heading_change():
    hist = [
        sample(41.28, 28.90, 120, t=NOW - 20),
        sample(41.27, 28.92, 125, t=NOW - 15),
        sample(41.25, 28.94, 130, t=NOW - 10),
        sample(41.23, 28.96, 135, t=NOW - 5),
        sample(41.21, 28.98, 140, t=NOW),
    ]
    evidence = turn_evidence(hist, now=NOW)
    assert evidence.stable
    assert evidence.sample_rates >= 3
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    diag = diagnostics_for(pred)
    assert diag["turn_shadow"]["used_turn"] is True
    assert diag["turn_shadow"]["turn_rate_deg_s"] > 0


def test_noisy_pseudo_turn_is_not_extrapolated():
    hist = [
        sample(41.30, 29.0, 180, t=NOW - 20),
        sample(41.28, 29.0, 181, t=NOW - 15),
        sample(41.26, 29.0, 179, t=NOW - 10),
        sample(41.24, 29.0, 181, t=NOW - 5),
        sample(41.22, 29.0, 179, t=NOW),
    ]
    evidence = turn_evidence(hist, now=NOW)
    assert not evidence.stable
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    assert diagnostics_for(pred)["turn_shadow"]["used_turn"] is False


def test_short_turn_history_is_not_extrapolated():
    hist = [
        sample(41.26, 29.0, 160, t=NOW - 10),
        sample(41.24, 29.0, 165, t=NOW - 5),
        sample(41.22, 29.0, 170, t=NOW),
    ]
    assert not turn_evidence(hist, now=NOW).stable


def test_stale_turn_history_is_not_extrapolated():
    hist = [
        sample(41.30, 29.0, 120, t=NOW - 45),
        sample(41.28, 29.0, 125, t=NOW - 40),
        sample(41.26, 29.0, 130, t=NOW - 35),
        sample(41.24, 29.0, 135, t=NOW - 30),
        sample(41.22, 29.0, 140, t=NOW - 25, age=25),
    ]
    evidence = turn_evidence(hist, now=NOW)
    assert not evidence.stable
    assert "fresh" in evidence.reason


def test_irregular_observations_reduce_confidence_and_explain_why():
    regular = [
        sample(41.30, 29.0, 180, t=NOW - 20),
        sample(41.28, 29.0, 180, t=NOW - 15),
        sample(41.26, 29.0, 180, t=NOW - 10),
        sample(41.24, 29.0, 180, t=NOW - 5),
        sample(41.22, 29.0, 180, t=NOW),
    ]
    irregular = [
        sample(41.30, 29.0, 180, t=NOW - 60),
        sample(41.28, 29.0, 180, t=NOW - 39),
        sample(41.26, 29.0, 180, t=NOW - 34),
        sample(41.24, 29.0, 180, t=NOW - 4),
        sample(41.22, 29.0, 180, t=NOW),
    ]
    p_regular = predict_trajectory_v46(regular, *USER, 15, now=NOW)
    p_irregular = predict_trajectory_v46(irregular, *USER, 15, now=NOW)
    d_regular = diagnostics_for(p_regular)["confidence"]
    d_irregular = diagnostics_for(p_irregular)["confidence"]
    assert d_irregular["score"] <= d_regular["score"]
    assert "irregular observation timing" in d_irregular["reasons"]


def test_ground_aircraft_requires_multiple_consistent_signals():
    hist = [
        sample(41.001, 29.001, 90, speed=30, t=NOW - 10, alt=100, vr=0),
        sample(41.001, 29.002, 90, speed=30, t=NOW - 5, alt=100, vr=0),
        sample(41.001, 29.003, 90, speed=30, t=NOW, alt=100, vr=0),
    ]
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    diag = diagnostics_for(pred)
    assert diag["confidence"]["ground_evidence"] is True
    assert pred.state == "Prediction uncertain"
    assert not pred.enters_alert_radius


def test_departing_low_aircraft_is_not_classified_as_ground():
    hist = [
        sample(41.001, 29.001, 90, speed=70, t=NOW - 10, alt=100, vr=6),
        sample(41.001, 29.005, 90, speed=90, t=NOW - 5, alt=140, vr=8),
        sample(41.001, 29.010, 90, speed=120, t=NOW, alt=200, vr=10),
    ]
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    assert diagnostics_for(pred)["confidence"]["ground_evidence"] is False


def test_altitude_context_keeps_horizontal_and_slant_cpa_separate():
    hist = [
        sample(41.20, 29.0, 180, t=NOW - 10, alt=10000),
        sample(41.16, 29.0, 180, t=NOW - 5, alt=10000),
        sample(41.12, 29.0, 180, t=NOW, alt=10000),
    ]
    pred = predict_trajectory_v46(hist, *USER, 15, now=NOW)
    assert pred.projected_closest_slant_km is not None
    assert pred.projected_closest_slant_km >= pred.projected_closest_km


def test_shadow_prediction_is_deterministic_and_does_not_change_production_geometry():
    hist = [
        sample(41.25, 28.95, 150, t=NOW - 20),
        sample(41.23, 28.96, 154, t=NOW - 15),
        sample(41.21, 28.97, 158, t=NOW - 10),
        sample(41.19, 28.98, 162, t=NOW - 5),
        sample(41.17, 28.99, 166, t=NOW),
    ]
    one = predict_trajectory_v46(hist, *USER, 12, now=NOW)
    d1 = diagnostics_for(one)
    two = predict_trajectory_v46(hist, *USER, 12, now=NOW)
    d2 = diagnostics_for(two)
    assert one.projected_closest_km == pytest.approx(two.projected_closest_km)
    assert one.time_to_cpa_s == pytest.approx(two.time_to_cpa_s)
    assert d1["linear_shadow"] == d2["linear_shadow"]
    assert d1["turn_shadow"] == d2["turn_shadow"]
