from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.intelligence import trajectory as trajectory
from app.intelligence.requalification_guard_v43 import CancellationLatch, _apply_latch
from app.intelligence.route_guard_v42 import RouteGateResultV42
from app.intelligence.trajectory_hotfix_v43 import predict_trajectory_v43
from app.version import PREDICTION_VERSION, VERSION


def _samples(*, position_age_s: float):
    now = 2_000_000_000.0
    return [
        trajectory.HistorySample(now - 10.0, 41.20, 29.0, 3000.0, 300.0, 180.0, 0.0, 0.0),
        trajectory.HistorySample(now, 41.16, 29.0, 3000.0, 300.0, 180.0, 0.0, position_age_s),
    ]


def test_v53_release_identity():
    assert tuple(int(part) for part in VERSION.split(".")) >= (5, 3, 0)
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"


def test_midpoint_eta_preserves_provider_position_age():
    now = 2_000_000_000.0
    fresh = predict_trajectory_v43(_samples(position_age_s=0.0), 41.0, 29.0, 15.0, now=now)
    delayed = predict_trajectory_v43(_samples(position_age_s=4.0), 41.0, 29.0, 15.0, now=now)
    assert fresh.time_to_cpa_s is not None
    assert delayed.time_to_cpa_s is not None
    assert delayed.time_to_cpa_s == pytest.approx(max(0.0, fresh.time_to_cpa_s - 4.0), abs=0.05)
    assert delayed.time_to_cpa_s <= fresh.time_to_cpa_s


def test_stale_inside_position_never_unlatches_cancelled_encounter():
    state = CancellationLatch(last_seen_mono=0.0, suppress_count=3, latched=True)
    recovered = RouteGateResultV42(suppress_alert=False, callsign="TEST1", reason="v4.2 QUALIFIED_PASS", qualification_state="QUALIFIED_PASS")
    stale_inside = SimpleNamespace(stale=True, current_distance_km=2.0)
    result = _apply_latch(recovered, pred=stale_inside, encounter_was_qualified=True, state=state, radius_km=9.0)
    assert result.suppress_alert is True
    assert result.qualification_state == "CANCEL_LATCHED"
    assert state.latched is True


def test_fresh_inside_position_still_unlatches_cancelled_encounter():
    state = CancellationLatch(last_seen_mono=0.0, suppress_count=3, latched=True)
    recovered = RouteGateResultV42(suppress_alert=False, callsign="TEST1", reason="v4.2 QUALIFIED_PASS", qualification_state="QUALIFIED_PASS")
    fresh_inside = SimpleNamespace(stale=False, current_distance_km=2.0)
    result = _apply_latch(recovered, pred=fresh_inside, encounter_was_qualified=True, state=state, radius_km=9.0)
    assert result.suppress_alert is False
    assert state.latched is False
    assert state.suppress_count == 0
