"""Regressions for the 2026-09-27 trajectory-chatter and empty-Next60 incident."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import inspect
from types import SimpleNamespace

from app.bot import next60
from app.intelligence.lifecycle import (
    INITIAL_ALERT_CONFIRMATIONS_REQUIRED,
    cancelled_latch_allows_reactivation,
    resolve_initial_alert_confirmation,
)
from app.worker import monitor


def _prediction(*, cpa: float = 5.0, eta: float = 180.0, current: float = 20.0, stale: bool = False):
    return SimpleNamespace(
        projected_closest_km=cpa,
        time_to_cpa_s=eta,
        current_distance_km=current,
        stale=stale,
    )


def test_initial_alert_needs_three_fresh_stable_predictions():
    pred = _prediction()
    count = 0
    previous_cpa = previous_eta = None
    ready = False
    for _ in range(INITIAL_ALERT_CONFIRMATIONS_REQUIRED):
        ready, count, previous_cpa, previous_eta = resolve_initial_alert_confirmation(
            count,
            previous_cpa,
            previous_eta,
            qualifies=True,
            fresh_observation=True,
            prediction=pred,
            alert_radius_km=8.0,
            observation_gap_s=5.0,
        )
    assert ready
    assert count == INITIAL_ALERT_CONFIRMATIONS_REQUIRED


def test_initial_alert_large_cpa_jump_restarts_confirmation_run():
    first = _prediction(cpa=4.5, eta=240)
    _, count, cpa, eta = resolve_initial_alert_confirmation(
        0, None, None,
        qualifies=True,
        fresh_observation=True,
        prediction=first,
        alert_radius_km=8.0,
        observation_gap_s=5.0,
    )
    second = _prediction(cpa=18.0, eta=220)
    ready, count, cpa, eta = resolve_initial_alert_confirmation(
        count, cpa, eta,
        qualifies=True,
        fresh_observation=True,
        prediction=second,
        alert_radius_km=8.0,
        observation_gap_s=5.0,
    )
    assert not ready
    assert count == 1
    assert cpa == 18.0


def test_initial_alert_provider_gap_cannot_stitch_old_evidence_to_new_snapshot():
    pred = _prediction(cpa=4.0, eta=180)
    _, count, cpa, eta = resolve_initial_alert_confirmation(
        0, None, None,
        qualifies=True,
        fresh_observation=True,
        prediction=pred,
        alert_radius_km=8.0,
        observation_gap_s=5.0,
    )
    ready, count, _, _ = resolve_initial_alert_confirmation(
        count, cpa, eta,
        qualifies=True,
        fresh_observation=True,
        prediction=pred,
        alert_radius_km=8.0,
        observation_gap_s=25.0,
    )
    assert not ready
    assert count == 1


def test_direct_observed_radius_entry_bypasses_initial_confirmation_delay():
    pred = _prediction(cpa=2.0, eta=20, current=7.5)
    ready, count, _, _ = resolve_initial_alert_confirmation(
        0, None, None,
        qualifies=True,
        fresh_observation=True,
        prediction=pred,
        alert_radius_km=8.0,
        observation_gap_s=None,
    )
    assert ready
    assert count == INITIAL_ALERT_CONFIRMATIONS_REQUIRED


def test_cancelled_alert_cannot_predictively_reactivate_outside_radius():
    pred = _prediction(current=14.0)
    assert not cancelled_latch_allows_reactivation("cancelled", False, pred, True, 8.0)
    pred.current_distance_km = 7.5
    assert cancelled_latch_allows_reactivation("cancelled", False, pred, True, 8.0)


def test_monitor_actually_wires_cancelled_latch_and_initial_confirmation():
    source = inspect.getsource(monitor._match_user_aircraft)
    assert "cancelled_latch_allows_reactivation(" in source
    assert "resolve_initial_alert_confirmation(" in source
    assert "initial_allowed and initial_ready" in source
    assert "approach_cancelled_latch" in source


def _live_state(now: datetime, *, age_s: float, candidate_state: str = "") -> dict:
    return {
        "aircraft_icao24": "4bb123",
        "route_callsign": "THY123",
        "aircraft_type": "A321",
        "projected_closest_km": 5.2,
        "time_to_cpa_s": 600.0,
        "prediction_at": now - timedelta(seconds=age_s),
        "confidence": "Medium",
        "stage": "prepare",
        "candidate_state": candidate_state,
    }


def test_next60_keeps_live_candidate_through_short_provider_gap():
    now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    doc = next60._live_state_doc(_live_state(now, age_s=60), now, {})
    assert doc is not None
    assert doc["callsign"] == "THY123"
    assert doc["source"] == "live"
    assert 0 < doc["prediction_horizon_s"] < 600


def test_next60_drops_old_live_candidate_after_bounded_gap():
    now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    assert next60._live_state_doc(_live_state(now, age_s=next60.LIVE_STATE_MAX_AGE_S + 1), now, {}) is None


def test_next60_drops_live_candidate_when_fresh_miss_evidence_exists():
    now = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)
    assert next60._live_state_doc(_live_state(now, age_s=5, candidate_state="Will not approach"), now, {}) is None


def test_next60_uses_full_bounded_route_retention_window():
    assert next60.HISTORY_DAYS == 7
    assert next60.MAX_HISTORY_ROUTES >= 3000
