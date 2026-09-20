from __future__ import annotations

import statistics
import time

from app.intelligence.trajectory import HistorySample, predict_trajectory

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def sample(lat, lon, *, alt=1000.0, heading=180.0, speed=420.0, vr=0.0, t=NOW, age=0.0):
    return HistorySample(t, lat, lon, alt, speed, heading, vr, age)


def direct_history(*, alt=1000.0, vr=0.0):
    return [
        sample(41.20, 29.0, alt=alt, vr=vr, t=NOW - 10),
        sample(41.18, 29.0, alt=alt + vr * 5, vr=vr, t=NOW - 5),
        sample(41.16, 29.0, alt=alt + vr * 10, vr=vr, t=NOW),
    ]


def test_low_altitude_nearby_pass_keeps_horizontal_and_3d_relevance():
    pred = predict_trajectory(direct_history(alt=700.0), *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km < 5.0
    assert pred.enters_alert_radius
    assert pred.three_d_available
    assert pred.observer_altitude_known
    assert not pred.altitude_relevance_applied


def test_directly_overhead_high_altitude_pass_can_be_rejected_by_true_3d_cpa():
    pred = predict_trajectory(direct_history(alt=12_000.0), *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km > 10.0
    assert pred.projected_closest_3d_lower_bound_km is not None
    assert pred.projected_closest_3d_lower_bound_km > 5.0
    assert not pred.enters_alert_radius
    assert pred.altitude_relevance_applied
    assert "3D CPA lower bound" in pred.reason


def test_crossing_track_retains_horizontal_cpa_and_reports_true_3d_separately():
    hist = [
        sample(41.04, 28.80, alt=1500.0, heading=90.0, t=NOW - 10),
        sample(41.04, 28.84, alt=1500.0, heading=90.0, t=NOW - 5),
        sample(41.04, 28.88, alt=1500.0, heading=90.0, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 6.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 6.0
    assert pred.projected_closest_3d_km is not None
    assert pred.projected_closest_3d_km >= pred.projected_closest_km
    assert pred.horizontal_at_3d_cpa_km is not None
    assert pred.vertical_at_3d_cpa_m is not None
    assert pred.enters_alert_radius


def test_descent_changes_true_3d_cpa_without_replacing_horizontal_cpa():
    hist = [
        sample(41.20, 29.0, alt=7000.0, heading=180.0, vr=-20.0, t=NOW - 10),
        sample(41.18, 29.0, alt=6900.0, heading=180.0, vr=-20.0, t=NOW - 5),
        sample(41.16, 29.0, alt=6800.0, heading=180.0, vr=-20.0, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_3d_km is not None
    assert pred.time_to_3d_cpa_s is not None
    assert pred.time_to_cpa_s is not None
    assert pred.vertical_at_3d_cpa_m is not None
    assert pred.projected_closest_km >= 0.0


def test_missing_altitude_fails_open_to_horizontal_prediction():
    hist = [
        sample(41.20, 29.0, alt=None, t=NOW - 10),
        sample(41.18, 29.0, alt=None, t=NOW - 5),
        sample(41.16, 29.0, alt=None, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.enters_alert_radius
    assert not pred.three_d_available
    assert pred.projected_closest_3d_km is None
    assert not pred.altitude_relevance_applied


def test_altitude_jump_anomaly_fails_open_instead_of_suppressing():
    hist = [
        sample(41.20, 29.0, alt=1000.0, t=NOW - 10),
        sample(41.18, 29.0, alt=1200.0, t=NOW - 5),
        sample(41.16, 29.0, alt=15_000.0, t=NOW),
    ]
    pred = predict_trajectory(hist, *USER, 5.0, now=NOW, user_altitude_m=100.0)
    assert pred.projected_closest_km < 2.0
    assert pred.enters_alert_radius
    assert not pred.three_d_available
    assert not pred.altitude_relevance_applied
    assert "discontinuity" in pred.altitude_relevance_reason


def test_unknown_observer_elevation_uses_global_envelope_and_fails_open_when_ambiguous():
    pred = predict_trajectory(direct_history(alt=12_000.0), *USER, 5.0, now=NOW)
    assert pred.projected_closest_km < 2.0
    assert pred.three_d_available
    assert not pred.observer_altitude_known
    assert pred.projected_closest_3d_km is None
    assert pred.projected_closest_3d_lower_bound_km is not None
    # A user could be at high terrain, so 5 km cannot safely be excluded.
    assert pred.enters_alert_radius
    assert not pred.altitude_relevance_applied


def test_altitude_relevance_can_be_disabled_without_changing_horizontal_geometry():
    pred = predict_trajectory(
        direct_history(alt=12_000.0), *USER, 5.0, now=NOW,
        user_altitude_m=100.0, altitude_relevance=False,
    )
    assert pred.projected_closest_km < 2.0
    assert pred.projected_closest_3d_km is not None
    assert pred.enters_alert_radius
    assert not pred.altitude_relevance_applied


def test_v51_geometry_overhead_remains_well_below_five_second_budget():
    hist = direct_history(alt=3500.0, vr=-3.0)
    timings = []
    for _ in range(500):
        started = time.perf_counter()
        pred = predict_trajectory(hist, *USER, 8.0, now=NOW, user_altitude_m=120.0)
        timings.append((time.perf_counter() - started) * 1000.0)
        assert pred.projected_closest_3d_km is not None
    timings.sort()
    p99 = timings[int(len(timings) * 0.99) - 1]
    assert statistics.median(timings) < 2.0
    assert p99 < 5.0
