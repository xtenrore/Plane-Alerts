from __future__ import annotations

from app.intelligence import direct_presence_guard_v44 as v44
from app.intelligence import prediction_v46 as v46
from app.intelligence import trajectory as trajectory
from app.intelligence import trajectory_hotfix_v43 as v43
from app.worker import critical_timing

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def _sample(lat: float, *, t: float, alt: float = 12000.0) -> trajectory.HistorySample:
    return trajectory.HistorySample(t, lat, 29.0, alt, 420.0, 180.0, 0.0, 0.0)


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

    monkeypatch.setattr(v43, "_ORIGINAL_PREDICT", spy_core)
    monkeypatch.setattr(v44, "_BASE_PREDICT", v43.predict_trajectory_v43)
    monkeypatch.setattr(v46, "_BASE_PREDICT", v44.predict_trajectory_v44)
    monkeypatch.setattr(critical_timing, "_ORIGINAL_PREDICT_TRAJECTORY", v46.predict_trajectory_v46)

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


def test_complete_wrapper_chain_keeps_unknown_observer_elevation(monkeypatch):
    samples = [
        _sample(41.20, t=NOW - 10, alt=900.0),
        _sample(41.18, t=NOW, alt=900.0),
    ]
    core_predict = trajectory.predict_trajectory
    monkeypatch.setattr(v43, "_ORIGINAL_PREDICT", core_predict)
    monkeypatch.setattr(v44, "_BASE_PREDICT", v43.predict_trajectory_v43)
    monkeypatch.setattr(v46, "_BASE_PREDICT", v44.predict_trajectory_v44)
    monkeypatch.setattr(critical_timing, "_ORIGINAL_PREDICT_TRAJECTORY", v46.predict_trajectory_v46)

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
