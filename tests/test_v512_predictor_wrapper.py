from __future__ import annotations

from app.intelligence import prediction_v46 as v46
from app.intelligence import trajectory as trajectory
from app.intelligence import trajectory_hotfix_v43 as v43
from app.worker import critical_timing

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def _sample(lat: float, *, t: float, alt: float = 12000.0) -> trajectory.HistorySample:
    return trajectory.HistorySample(t, lat, 29.0, alt, 420.0, 180.0, 0.0, 0.0)


def test_production_wrapper_chain_accepts_and_forwards_altitude_relevance(monkeypatch):
    """Regression for the v5.1.1 production TypeError in the real wrapper order."""
    samples = [
        _sample(41.20, t=NOW - 10),
        _sample(41.18, t=NOW),
    ]
    base_predict = v46._BASE_PREDICT
    forwarded: list[bool] = []

    def spy_base(*args, **kwargs):
        forwarded.append(bool(kwargs["altitude_relevance"]))
        return base_predict(*args, **kwargs)

    monkeypatch.setattr(v46, "_BASE_PREDICT", spy_base)
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


def test_v46_wrapper_keeps_unknown_observer_elevation_as_unknown():
    samples = [
        _sample(41.20, t=NOW - 10, alt=900.0),
        _sample(41.18, t=NOW, alt=900.0),
    ]
    pred = v46.predict_trajectory_v46(
        samples,
        *USER,
        8.0,
        now=NOW,
        user_altitude_m=None,
        altitude_relevance=True,
    )
    assert pred.observer_altitude_known is False


def test_legacy_v43_wrapper_accepts_unknown_observer_elevation():
    """Regression for the production float-minus-None crash after compatibility retry."""
    samples = [
        _sample(41.20, t=NOW - 10, alt=900.0),
        _sample(41.18, t=NOW, alt=900.0),
    ]
    pred = v43.predict_trajectory_v43(
        samples,
        *USER,
        8.0,
        now=NOW,
        user_altitude_m=None,
    )
    assert pred.observer_altitude_known is False
    assert pred.projected_closest_km >= 0.0
