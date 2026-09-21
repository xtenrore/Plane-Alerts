from __future__ import annotations

from pathlib import Path

from app.intelligence import direct_presence_guard_v44 as v44
from app.intelligence import midpoint_scale_v53 as midpoint_v53
from app.intelligence import prediction_v46 as v46
from app.intelligence import trajectory as trajectory
from app.intelligence import trajectory_hotfix_v43 as v43
from app.intelligence import trajectory_scale_v53 as scale_v53
from app.worker import critical_timing

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def _sample(lat: float, *, t: float, alt: float = 12000.0) -> trajectory.HistorySample:
    return trajectory.HistorySample(t, lat, 29.0, alt, 420.0, 180.0, 0.0, 0.0)


def _install_chain(monkeypatch, *, core=None):
    core_predict = core or trajectory.predict_trajectory
    monkeypatch.setattr(v43, "_ORIGINAL_PREDICT", core_predict)
    monkeypatch.setattr(v44, "_BASE_PREDICT", v43.predict_trajectory_v43)
    monkeypatch.setattr(v46, "_BASE_PREDICT", v44.predict_trajectory_v44)
    monkeypatch.setattr(critical_timing, "_ORIGINAL_PREDICT_TRAJECTORY", v46.predict_trajectory_v46)
    return core_predict


def test_complete_production_wrapper_chain_forwards_altitude_relevance(monkeypatch):
    """Recreate core -> v4.3 -> v4.4 -> v4.6 -> critical timing exactly."""
    samples = [
        _sample(41.20, t=NOW - 10),
        _sample(41.18, t=NOW),
    ]
    core_predict = trajectory.predict_trajectory
    forwarded: list[bool] = []

    def spy_core(*args, **kwargs):
        forwarded.append(bool(kwargs["altitude_relevance"]))
        return core_predict(*args, **kwargs)

    _install_chain(monkeypatch, core=spy_core)

    disabled = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        5.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=False,
    )
    enabled = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        5.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=True,
    )

    assert forwarded == [False, True]
    assert disabled.enters_alert_radius
    assert disabled.altitude_relevance_applied is False
    assert not enabled.enters_alert_radius
    assert enabled.altitude_relevance_applied is True


def test_v44_direct_presence_does_not_undo_trustworthy_3d_exclusion(monkeypatch):
    samples = [
        _sample(41.055, t=NOW - 10, alt=12000.0),
        _sample(41.040, t=NOW, alt=12000.0),
    ]
    _install_chain(monkeypatch)

    pred = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        5.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=True,
    )

    assert pred.current_distance_km < 5.0
    assert pred.altitude_relevance_applied is True
    assert pred.enters_alert_radius is False
    assert pred.state == "Will not approach"


def test_complete_wrapper_chain_keeps_unknown_observer_elevation(monkeypatch):
    samples = [
        _sample(41.20, t=NOW - 10, alt=900.0),
        _sample(41.18, t=NOW, alt=900.0),
    ]
    _install_chain(monkeypatch)

    pred = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        8.0,
        now=NOW,
        user_altitude_m=None,
        altitude_relevance=True,
    )
    assert pred.observer_altitude_known is False
    assert pred.projected_closest_km >= 0.0


def test_each_legacy_wrapper_accepts_current_v51_signature(monkeypatch):
    samples = [
        _sample(41.20, t=NOW - 10, alt=900.0),
        _sample(41.18, t=NOW, alt=900.0),
    ]
    core_predict = trajectory.predict_trajectory
    monkeypatch.setattr(v43, "_ORIGINAL_PREDICT", core_predict)
    monkeypatch.setattr(v44, "_BASE_PREDICT", v43.predict_trajectory_v43)

    p43 = v43.predict_trajectory_v43(
        samples,
        *USER,
        8.0,
        now=NOW,
        user_altitude_m=None,
        altitude_relevance=False,
    )
    p44 = v44.predict_trajectory_v44(
        samples,
        *USER,
        8.0,
        now=NOW,
        user_altitude_m=None,
        altitude_relevance=False,
    )
    assert p43.observer_altitude_known is False
    assert p44.observer_altitude_known is False


def test_full_chain_does_not_restore_projected_cpa_pass_shortcut(monkeypatch):
    samples = [
        _sample(41.10, t=NOW - 10, alt=1200.0),
        _sample(41.11, t=NOW - 5, alt=1200.0),
        _sample(41.12, t=NOW, alt=1200.0),
    ]
    _install_chain(monkeypatch)

    pred = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        5.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=True,
    )

    assert pred.already_passed is False
    assert pred.state != "Passed"


def test_full_chain_preserves_observed_in_radius_pass(monkeypatch):
    samples = [
        _sample(41.012, t=NOW - 10, alt=1200.0),
        _sample(41.020, t=NOW - 5, alt=1200.0),
        _sample(41.030, t=NOW, alt=1200.0),
    ]
    _install_chain(monkeypatch)

    pred = critical_timing._critical_predict_trajectory(
        samples,
        *USER,
        2.0,
        now=NOW,
        user_altitude_m=100.0,
        altitude_relevance=True,
    )

    assert pred.already_passed is True
    assert pred.state == "Passed"


def _assert_same_prediction(left, right):
    assert left.state == right.state
    assert left.confidence == right.confidence
    assert left.enters_alert_radius == right.enters_alert_radius
    assert left.already_passed == right.already_passed
    assert left.turning_away == right.turning_away
    assert left.altitude_relevance_applied == right.altitude_relevance_applied
    for field in (
        "confidence_score",
        "current_distance_km",
        "projected_closest_km",
        "projected_closest_slant_km",
        "time_to_cpa_s",
        "radius_entry_s",
        "projected_closest_3d_km",
        "projected_closest_3d_lower_bound_km",
        "time_to_3d_cpa_s",
    ):
        a = getattr(left, field)
        b = getattr(right, field)
        if a is None or b is None:
            assert a is b
        else:
            assert abs(float(a) - float(b)) < 1e-9
    assert len(left.path) == len(right.path)
    for a, b in zip(left.path, right.path):
        assert a.seconds == b.seconds
        assert abs(a.latitude - b.latitude) < 1e-12
        assert abs(a.longitude - b.longitude) < 1e-12
        assert abs(a.horizontal_km - b.horizontal_km) < 1e-9


def test_v53_shared_base_preserves_complete_wrapper_output(monkeypatch):
    samples = [
        trajectory.HistorySample(NOW - 15, 41.20, 28.95, 6000.0, 410.0, 173.0, -2.0, 0.0),
        trajectory.HistorySample(NOW - 10, 41.18, 28.96, 5990.0, 412.0, 176.0, -2.0, 0.0),
        trajectory.HistorySample(NOW - 5, 41.16, 28.97, 5980.0, 414.0, 179.0, -2.0, 0.0),
        trajectory.HistorySample(NOW, 41.14, 28.98, 5970.0, 416.0, 182.0, -2.0, 0.0),
    ]

    scale_v53.reset_motion_cache_for_tests()
    midpoint_v53.reset_midpoint_cache_for_tests()
    _install_chain(monkeypatch, core=trajectory.predict_trajectory)
    expected = critical_timing._critical_predict_trajectory(
        samples, 41.0, 29.0, 15.0, now=NOW, user_altitude_m=100.0, altitude_relevance=True
    )

    scale_v53.reset_motion_cache_for_tests()
    midpoint_v53.reset_midpoint_cache_for_tests()
    _install_chain(monkeypatch, core=scale_v53.predict_trajectory)
    actual = critical_timing._critical_predict_trajectory(
        samples, 41.0, 29.0, 15.0, now=NOW, user_altitude_m=100.0, altitude_relevance=True
    )
    _assert_same_prediction(expected, actual)

    # A second observer reuses both absolute paths, while their CPA remains
    # independently evaluated by the full safety wrapper chain.
    critical_timing._critical_predict_trajectory(
        samples, 41.05, 29.05, 15.0, now=NOW, user_altitude_m=80.0, altitude_relevance=True
    )
    assert scale_v53.motion_cache_snapshot()["hits"] >= 1
    assert midpoint_v53.midpoint_cache_snapshot()["hits"] >= 1


def test_v53_production_install_order_keeps_safety_wrappers_above_shared_base():
    worker_init = Path("app/worker/__init__.py").read_text(encoding="utf-8")
    v35_source = Path("app/worker/v35.py").read_text(encoding="utf-8")
    shared_install = worker_init.index("_trajectory_core.predict_trajectory = _predict_trajectory_v53")
    v43_install = worker_init.index("install_trajectory_hotfix_v43()")
    v44_install = worker_init.index("install_direct_presence_guard_v44()")
    v46_install = worker_init.index("install_prediction_v46()")
    critical_install = worker_init.index("install_critical_timing_guards()")
    assert shared_install < v43_install < v44_install < v46_install < critical_install
    assert "monitor.predict_trajectory = predict_trajectory_v53" not in v35_source
