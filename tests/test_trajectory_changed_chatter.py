"""Regression coverage for excessive TRAJECTORY CHANGED alert chatter."""
from __future__ import annotations

from app.intelligence.lifecycle import advance_cancellation_confirmation
from app.intelligence.trajectory import HistorySample, predict_trajectory

NOW = 2_000_000_000.0
USER = (41.0, 29.0)


def _sample(lat: float, *, t: float, heading: float = 180.0) -> HistorySample:
    return HistorySample(
        timestamp=t,
        latitude=lat,
        longitude=29.0,
        altitude_m=3500.0,
        speed_kts=420.0,
        heading_deg=heading,
        vertical_rate_mps=0.0,
        position_age_s=0.0,
    )


def test_one_or_two_outside_samples_cannot_reach_default_low_alert_confidence():
    one = predict_trajectory([_sample(41.20, t=NOW)], *USER, 8.0, now=NOW)
    two = predict_trajectory(
        [_sample(41.20, t=NOW - 5), _sample(41.18, t=NOW)],
        *USER,
        8.0,
        now=NOW,
    )

    # Geometry is still calculated so explicit Uncertain-profile users can opt
    # into it, but the normal Low+ alert path must wait for observed track
    # evidence instead of trusting a one/two-point feed vector.
    assert one.enters_alert_radius
    assert two.enters_alert_radius
    assert one.confidence_score < 0.36
    assert two.confidence_score < 0.36
    assert one.confidence == "Uncertain"
    assert two.confidence == "Uncertain"


def test_third_contiguous_sample_unlocks_normal_confidence_when_motion_is_stable():
    pred = predict_trajectory(
        [
            _sample(41.20, t=NOW - 10),
            _sample(41.18, t=NOW - 5),
            _sample(41.16, t=NOW),
        ],
        *USER,
        8.0,
        now=NOW,
    )

    assert pred.enters_alert_radius
    assert pred.confidence_score >= 0.36
    assert pred.confidence in {"Low", "Medium", "High"}


def test_fresh_non_candidate_breaks_cancellation_confirmation_streak():
    confirmed, count = advance_cancellation_confirmation(0, True)
    assert not confirmed and count == 1

    confirmed, count = advance_cancellation_confirmation(count, True)
    assert not confirmed and count == 2

    # Mirrors the production sequence where an uncertain fresh prediction sat
    # between miss candidates. It must break the streak instead of carrying two
    # old strikes into a later cancellation.
    confirmed, count = advance_cancellation_confirmation(count, False)
    assert not confirmed and count == 0

    confirmed, count = advance_cancellation_confirmation(count, True)
    assert not confirmed and count == 1
    confirmed, count = advance_cancellation_confirmation(count, True)
    assert not confirmed and count == 2
    confirmed, count = advance_cancellation_confirmation(count, True)
    assert confirmed and count == 3
