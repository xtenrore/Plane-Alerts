from dataclasses import dataclass

import pytest

from app.worker import critical_timing


@dataclass
class _Prediction:
    state: str = "Approaching"
    confidence: str = "High"
    confidence_score: float = 0.9
    enters_alert_radius: bool = True
    stale: bool = False
    reason: str = "ok"


@dataclass
class _Sample:
    timestamp: float
    position_age_s: float = 0.0
    speed_kts: float = 400.0


def test_altitude_relevance_compatibility_does_not_drop_cpa(monkeypatch):
    calls = []

    def legacy_predict(samples, user_lat, user_lon, alert_radius_km, *, now=None, user_altitude_m=0.0):
        calls.append((now, user_altitude_m))
        return _Prediction()

    monkeypatch.setattr(critical_timing, "_ORIGINAL_PREDICT_TRAJECTORY", legacy_predict)
    sample = _Sample(timestamp=1000.0)

    result = critical_timing._critical_predict_trajectory(
        [sample],
        41.0,
        29.0,
        12.0,
        now=1000.0,
        user_altitude_m=100.0,
        altitude_relevance=True,
    )

    assert result.state == "Approaching"
    assert calls == [(1000.0, 100.0)]


def test_unrelated_type_error_is_not_swallowed(monkeypatch):
    def broken_predict(*args, **kwargs):
        raise TypeError("internal predictor bug")

    monkeypatch.setattr(critical_timing, "_ORIGINAL_PREDICT_TRAJECTORY", broken_predict)

    with pytest.raises(TypeError, match="internal predictor bug"):
        critical_timing._critical_predict_trajectory(
            [_Sample(timestamp=1000.0)],
            41.0,
            29.0,
            12.0,
            now=1000.0,
            altitude_relevance=True,
        )
