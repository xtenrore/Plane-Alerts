from dataclasses import dataclass

from app.worker.critical_timing import (
    _MISSING_SPEED_FRESHNESS_S,
    _harden_prediction_freshness,
    _live_freshness_limit,
)


@dataclass(frozen=True)
class Prediction:
    state: str = "Approaching"
    confidence: str = "High"
    confidence_score: float = 0.9
    enters_alert_radius: bool = True
    stale: bool = False
    reason: str = "live CPA"


@dataclass(frozen=True)
class Sample:
    timestamp: float
    position_age_s: float
    speed_kts: float | None = None


def test_missing_speed_keeps_conservative_fallback_and_blocks_old_continuity_position():
    now = 1000.0
    prediction = Prediction()
    sample = Sample(timestamp=986.2, position_age_s=13.8, speed_kts=None)
    hardened = _harden_prediction_freshness(prediction, [sample], now=now)

    assert _MISSING_SPEED_FRESHNESS_S == 12.0
    assert _live_freshness_limit([sample]) == 12.0
    assert hardened.stale is True
    assert hardened.enters_alert_radius is False
    assert hardened.state == "Prediction uncertain"
    assert "13.8s" in hardened.reason
    assert "speed-dependent" in hardened.reason


def test_high_speed_aircraft_becomes_uncertain_quickly():
    now = 1000.0
    prediction = Prediction()
    sample = Sample(timestamp=990.0, position_age_s=10.0, speed_kts=800.0)
    assert _live_freshness_limit([sample]) < 10.0
    hardened = _harden_prediction_freshness(prediction, [sample], now=now)
    assert hardened.stale is True
    assert hardened.enters_alert_radius is False


def test_slow_aircraft_can_remain_fresh_longer_with_speed_evidence():
    now = 1000.0
    prediction = Prediction()
    sample = Sample(timestamp=980.0, position_age_s=20.0, speed_kts=100.0)
    assert _live_freshness_limit([sample]) == 30.0
    hardened = _harden_prediction_freshness(prediction, [sample], now=now)
    assert hardened is prediction


def test_fresh_position_keeps_live_prediction_unchanged():
    prediction = Prediction()
    hardened = _harden_prediction_freshness(
        prediction,
        [Sample(timestamp=997.0, position_age_s=2.0, speed_kts=450.0)],
        now=1000.0,
    )
    assert hardened is prediction
