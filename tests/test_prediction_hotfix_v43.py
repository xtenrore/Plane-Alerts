import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.intelligence.direct_presence_guard_v44 import _confirmed_fresh_direct_presence
from app.intelligence.lifecycle import should_cancel_active_alert
from app.intelligence.requalification_guard_v43 import (
    CancellationLatch,
    _apply_latch,
)
from app.intelligence import route_observe_guard_v44
from app.intelligence.route_guard_v42 import RouteGateResultV42
from app.intelligence.trajectory import HistorySample
from app.intelligence.trajectory_hotfix_v43 import (
    _midpoint_motion_step,
    predict_trajectory_v43,
)
from app.next_hour_shadow_v43 import _history_quality_reason


def test_low_confidence_turn_away_can_accumulate_cancellation_evidence():
    prediction = SimpleNamespace(
        stale=False,
        already_passed=False,
        state="Turning away",
        enters_alert_radius=False,
        projected_closest_km=20.0,
        distance_trend_km_s=0.006,
        turning_away=True,
        confidence_score=0.20,
    )
    assert should_cancel_active_alert(prediction, 5.0, 9.0)


def test_cancelled_encounter_cannot_predictively_requalify_until_inside_radius():
    state = CancellationLatch(last_seen_mono=0.0)
    outside = SimpleNamespace(stale=False, current_distance_km=12.0)
    suppressed = RouteGateResultV42(
        suppress_alert=True,
        callsign="THY1017",
        reason="v4.2 ACTIVE_BELOW_CANCEL_HYSTERESIS",
        qualification_state="ACTIVE_BELOW_CANCEL_HYSTERESIS",
    )

    for _ in range(2):
        result = _apply_latch(
            suppressed,
            pred=outside,
            encounter_was_qualified=True,
            state=state,
            radius_km=9.0,
        )
        assert result.suppress_alert
        assert not state.latched

    result = _apply_latch(
        suppressed,
        pred=outside,
        encounter_was_qualified=True,
        state=state,
        radius_km=9.0,
    )
    assert state.latched
    assert result.qualification_state == "CANCEL_LATCHED"

    recovered_prediction = RouteGateResultV42(
        suppress_alert=False,
        callsign="THY1017",
        reason="v4.2 QUALIFIED_PASS",
        qualification_state="QUALIFIED_PASS",
    )
    result = _apply_latch(
        recovered_prediction,
        pred=outside,
        encounter_was_qualified=True,
        state=state,
        radius_km=9.0,
    )
    assert result.suppress_alert
    assert result.qualification_state == "CANCEL_LATCHED"

    inside = SimpleNamespace(stale=False, current_distance_km=8.8)
    result = _apply_latch(
        recovered_prediction,
        pred=inside,
        encounter_was_qualified=True,
        state=state,
        radius_km=9.0,
    )
    assert not result.suppress_alert
    assert not state.latched
    assert state.suppress_count == 0


def test_midpoint_step_uses_average_speed_and_heading():
    next_speed, next_heading, midpoint_speed, midpoint_heading = _midpoint_motion_step(
        100.0,
        359.0,
        acceleration_kts_s=1.0,
        turn_rate_deg_s=1.0,
        acceleration_factor=1.0,
        turn_factor=1.0,
        step_s=3.0,
    )
    assert next_speed == pytest.approx(103.0)
    assert midpoint_speed == pytest.approx(101.5)
    assert next_heading == pytest.approx(2.0)
    assert midpoint_heading == pytest.approx(0.5)


def test_midpoint_predictor_marks_observed_in_radius_receding_motion_as_passed():
    samples = [
        HistorySample(
            timestamp=100.0,
            latitude=0.035,
            longitude=0.0,
            altitude_m=3000.0,
            speed_kts=260.0,
            heading_deg=0.0,
        ),
        HistorySample(
            timestamp=110.0,
            latitude=0.045,
            longitude=0.0,
            altitude_m=3000.0,
            speed_kts=260.0,
            heading_deg=0.0,
        ),
    ]
    prediction = predict_trajectory_v43(
        samples,
        0.0,
        0.0,
        9.0,
        now=110.0,
    )
    assert prediction.current_distance_km < 9.0
    assert prediction.already_passed
    assert prediction.state == "Passed"


def test_midpoint_predictor_keeps_fresh_approaching_in_radius_presence_authoritative():
    samples = [
        HistorySample(
            timestamp=100.0,
            latitude=0.055,
            longitude=0.0,
            altitude_m=3000.0,
            speed_kts=260.0,
            heading_deg=180.0,
        ),
        HistorySample(
            timestamp=110.0,
            latitude=0.045,
            longitude=0.0,
            altitude_m=3000.0,
            speed_kts=260.0,
            heading_deg=180.0,
        ),
    ]
    prediction = predict_trajectory_v43(
        samples,
        0.0,
        0.0,
        9.0,
        now=110.0,
    )
    assert prediction.current_distance_km < 9.0
    assert prediction.enters_alert_radius
    assert not prediction.already_passed
    assert prediction.state == "Passing nearby"
    assert prediction.radius_entry_s == 0.0


def test_direct_presence_requires_two_fresh_independent_positions_and_approach():
    samples = [
        HistorySample(timestamp=90.0, latitude=0.085, longitude=0.0, position_age_s=0.0),
        HistorySample(timestamp=100.0, latitude=0.070, longitude=0.0, position_age_s=0.0),
    ]
    uncertain = SimpleNamespace(time_to_cpa_s=None, projected_closest_km=7.8)
    assert _confirmed_fresh_direct_presence(
        samples,
        user_lat=0.0,
        user_lon=0.0,
        alert_radius_km=9.0,
        now=100.0,
        prediction=uncertain,
    )


def test_direct_presence_rejects_single_stale_duplicate_or_moving_away_evidence():
    uncertain = SimpleNamespace(time_to_cpa_s=None, projected_closest_km=3.0)
    single = [HistorySample(timestamp=100.0, latitude=0.070, longitude=0.0)]
    assert not _confirmed_fresh_direct_presence(
        single,
        user_lat=0.0,
        user_lon=0.0,
        alert_radius_km=9.0,
        now=100.0,
        prediction=uncertain,
    )

    stale = [
        HistorySample(timestamp=70.0, latitude=0.085, longitude=0.0, position_age_s=30.0),
        HistorySample(timestamp=75.0, latitude=0.070, longitude=0.0, position_age_s=25.0),
    ]
    assert not _confirmed_fresh_direct_presence(
        stale,
        user_lat=0.0,
        user_lon=0.0,
        alert_radius_km=9.0,
        now=100.0,
        prediction=uncertain,
    )

    duplicate = [
        HistorySample(timestamp=95.0, latitude=0.070, longitude=0.0),
        HistorySample(timestamp=100.0, latitude=0.070, longitude=0.0),
    ]
    assert not _confirmed_fresh_direct_presence(
        duplicate,
        user_lat=0.0,
        user_lon=0.0,
        alert_radius_km=9.0,
        now=100.0,
        prediction=uncertain,
    )

    moving_away = [
        HistorySample(timestamp=95.0, latitude=0.050, longitude=0.0),
        HistorySample(timestamp=100.0, latitude=0.070, longitude=0.0),
    ]
    assert not _confirmed_fresh_direct_presence(
        moving_away,
        user_lat=0.0,
        user_lon=0.0,
        alert_radius_km=9.0,
        now=100.0,
        prediction=uncertain,
    )


@pytest.mark.asyncio
async def test_route_history_background_write_is_bounded(monkeypatch):
    async def slow_observe(_self, _ac, *, now=None):
        await asyncio.sleep(0.05)

    monkeypatch.setattr(route_observe_guard_v44, "_BASE_OBSERVE", slow_observe)
    monkeypatch.setattr(route_observe_guard_v44, "_OBSERVE_TIMEOUT_S", 0.005)
    await asyncio.wait_for(
        route_observe_guard_v44._bounded_original_observe(
            SimpleNamespace(),
            SimpleNamespace(callsign="THY1017"),
            now=100.0,
        ),
        timeout=0.1,
    )


@pytest.mark.asyncio
async def test_route_history_queue_uses_fixed_workers_and_dedupes(monkeypatch):
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def held_observe(_self, ac, *, now=None):
        calls.append((ac.callsign, now))
        started.set()
        await release.wait()

    monkeypatch.setattr(route_observe_guard_v44, "_BASE_OBSERVE", held_observe)
    monkeypatch.setattr(route_observe_guard_v44, "_OBSERVE_TIMEOUT_S", 1.0)
    service = SimpleNamespace(_last_sample={})
    aircraft = SimpleNamespace(
        callsign="THY1017",
        latitude=41.0,
        longitude=29.0,
        altitude=3000.0,
        heading=180.0,
    )

    await route_observe_guard_v44.observe_queued(service, aircraft, now=100.0)
    await asyncio.wait_for(started.wait(), timeout=0.1)
    await route_observe_guard_v44.observe_queued(service, aircraft, now=100.0)

    assert len(service._route_guard_v44_observe_workers) == route_observe_guard_v44._OBSERVE_WORKERS
    assert service._route_guard_v44_observe_queue.maxsize == route_observe_guard_v44._OBSERVE_QUEUE_LIMIT
    assert len(calls) == 1
    assert "THY1017" in service._route_guard_v44_observe_keys

    release.set()
    await asyncio.wait_for(service._route_guard_v44_observe_queue.join(), timeout=0.2)
    assert calls == [("THY1017", 100.0)]
    assert not service._route_guard_v44_observe_keys

    workers = service._route_guard_v44_observe_workers
    for worker in workers:
        worker.cancel()
    await asyncio.gather(*workers, return_exceptions=True)


def test_route_history_guard_replaces_v2_task_fanout_with_queue():
    source = Path("app/intelligence/route_observe_guard_v44.py").read_text()
    assert "RouteHistoryService.observe = observe_queued" in source
    assert "asyncio.Queue(maxsize=_OBSERVE_QUEUE_LIMIT)" in source
    assert "_OBSERVE_WORKERS = 6" in source


def test_next_hour_shadow_rejects_single_day_and_ambiguous_history():
    assert _history_quality_reason(1, 0.0) == "insufficient_history_days"
    assert _history_quality_reason(2, 1900.0) == "ambiguous_or_multimodal_time_history"
    assert _history_quality_reason(2, 900.0) == ""


def test_worker_installs_direct_presence_and_bounded_route_writes_before_monitor_imports():
    worker_init = Path("app/worker/__init__.py").read_text()
    assert "install_direct_presence_guard_v44()" in worker_init
    assert "install_route_observe_guard_v44()" in worker_init
