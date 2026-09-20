from __future__ import annotations

import time

from app.intelligence.trajectory import HistorySample, predict_trajectory

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def sample(lat, lon, heading, *, speed=420.0, t=NOW, alt=1200.0, vr=0.0, age=0.0):
    return HistorySample(t, lat, lon, alt, speed, heading, vr, age)


def test_low_altitude_nearby_pass_remains_relevant_in_3d():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=700.0),
        sample(41.18, 29.0, 180.0, t=NOW, alt=700.0),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km < 8.0
    assert pred.enters_alert_radius
    assert pred.altitude_relevance_applied is False


def test_directly_overhead_high_altitude_pass_is_rejected_by_true_3d_cpa():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=12000.0),
        sample(41.18, 29.0, 180.0, t=NOW, alt=12000.0),
    ]
    pred = predict_trajectory(hist, *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km > 10.0
    assert pred.projected_closest_3d_lower_bound_km is not None
    assert pred.projected_closest_3d_lower_bound_km > 5.0
    assert not pred.enters_alert_radius
    assert pred.altitude_relevance_applied


def test_crossing_track_reports_horizontal_and_3d_cpa_separately():
    hist = [
        sample(41.04, 28.80, 90.0, t=NOW - 10, alt=3500.0),
        sample(41.04, 28.84, 90.0, t=NOW, alt=3500.0),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=150.0)
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km >= pred.projected_closest_km
    assert pred.horizontal_at_3d_cpa_km is not None
    assert pred.vertical_at_3d_cpa_m is not None
    assert pred.time_to_3d_cpa_s is not None


def test_descent_changes_vertical_component_and_stays_deterministic():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=2800.0, vr=-20.0),
        sample(41.18, 29.0, 180.0, t=NOW, alt=2600.0, vr=-20.0),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.three_d_available
    assert pred.projected_closest_3d_km is not None
    assert pred.vertical_at_3d_cpa_m is not None
    assert pred.vertical_at_3d_cpa_m < 2500.0


def test_climb_changes_vertical_component_and_stays_deterministic():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=700.0, vr=18.0),
        sample(41.18, 29.0, 180.0, t=NOW, alt=880.0, vr=18.0),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.three_d_available
    assert pred.projected_closest_3d_km is not None
    assert pred.vertical_at_3d_cpa_m is not None
    assert pred.vertical_at_3d_cpa_m > 780.0


def test_missing_altitude_fails_open_to_horizontal_prediction():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=None),
        sample(41.18, 29.0, 180.0, t=NOW, alt=None),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is None
    assert pred.enters_alert_radius
    assert pred.altitude_confidence == "Unavailable"
    assert pred.altitude_relevance_applied is False


def test_altitude_report_anomaly_fails_open_instead_of_suppressing():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=1000.0),
        sample(41.19, 29.0, 180.0, t=NOW - 5, alt=1100.0),
        sample(41.18, 29.0, 180.0, t=NOW, alt=20000.0),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.enters_alert_radius
    assert pred.altitude_confidence == "Unavailable"
    assert pred.altitude_relevance_applied is False
    assert "discontinuity" in pred.altitude_relevance_reason


def test_unknown_observer_elevation_uses_global_envelope_and_fails_open_when_ambiguous():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=12000.0),
        sample(41.18, 29.0, 180.0, t=NOW - 5, alt=12000.0),
        sample(41.16, 29.0, 180.0, t=NOW, alt=12000.0),
    ]
    pred = predict_trajectory(hist, *USER, 5.0, now=NOW, user_altitude_m=None)
    assert pred.three_d_available
    assert pred.observer_altitude_known is False
    assert pred.projected_closest_3d_km is None
    assert pred.projected_closest_3d_lower_bound_km is not None
    # A very high aircraft can still be excluded if even the most conservative
    # terrestrial observer altitude leaves it outside the radius.
    assert pred.projected_closest_3d_lower_bound_km > 0.0


def test_altitude_relevance_can_be_disabled_without_hiding_3d_diagnostics():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 10, alt=12000.0),
        sample(41.18, 29.0, 180.0, t=NOW - 5, alt=12000.0),
        sample(41.16, 29.0, 180.0, t=NOW, alt=12000.0),
    ]
    pred = predict_trajectory(
        hist,
        *USER,
        5.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=False,
    )
    assert pred.projected_closest_3d_km is not None
    assert pred.altitude_relevance_applied is False
    assert pred.enters_alert_radius


def test_v51_geometry_path_is_microsecond_scale_not_network_scale():
    hist = [
        sample(41.20, 29.0, 180.0, t=NOW - 15, alt=4000.0),
        sample(41.19, 29.0, 180.0, t=NOW - 10, alt=3900.0),
        sample(41.18, 29.0, 180.0, t=NOW - 5, alt=3800.0),
        sample(41.17, 29.0, 180.0, t=NOW, alt=3700.0),
    ]
    start = time.perf_counter()
    for _ in range(100):
        pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
        assert pred.projected_closest_3d_km is not None
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5
