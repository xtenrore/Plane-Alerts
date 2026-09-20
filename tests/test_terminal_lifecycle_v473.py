"""Decision-sequence regressions for the v4.7.2 initial-alert bypasses.

These are code-derived reproductions, not reconstructed raw production tracks.
"""
from types import SimpleNamespace
from types import MethodType
from unittest.mock import AsyncMock
from datetime import datetime, timezone

import pytest

from app.intelligence import route_guard_v42 as v42
from app.intelligence import route_guard_v47 as v47
from app.intelligence import terminal_arrival_hold_v472 as hold
from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence import global_airports_v471 as global_airports
from app.intelligence import route_history, trajectory
from test_route_guard_v47 import _assessment, _pred
from app.intelligence import requalification_guard_v43 as v43
from app.worker import monitor


@pytest.fixture(autouse=True)
def reset():
    v47.reset_v472_initial_hold_state_for_tests()
    v43.reset_v43_requalification_state_for_tests()
    v42._encounters.clear()
    yield
    v47.reset_v472_initial_hold_state_for_tests()
    v43.reset_v43_requalification_state_for_tests()
    v42._encounters.clear()


@pytest.mark.parametrize("initial_code", ["INSUFFICIENT_HISTORY", "RELEASE_NOT_FRESH", "NO_STRONG_AIRPORT_IDENTITY"])
def test_early_missing_evidence_cannot_disable_later_arrival_hold(initial_code):
    base = v42.RouteGateResultV42(False, "TEST1", "candidate only", qualification_state="QUALIFIED_PASS")
    v47._apply_initial_hold(
        base=base, decision=hold.TerminalArrivalHoldDecision(False, initial_code, "not ready"),
        notification_sent=False,
    )
    result, effective = v47._apply_initial_hold(
        base=base, decision=hold.TerminalArrivalHoldDecision(True, "HOLD_STRONG_TERMINAL_ARRIVAL", "fresh arrival"),
        notification_sent=False,
    )
    assert effective and result.suppress_alert


@pytest.mark.parametrize("code", ["RELEASE_TRAJECTORY_CHANGED", "RELEASE_CLIMBING", "RELEASE_RUNWAY_PATH_CAN_PASS"])
def test_weak_release_signal_cannot_overrule_confirmed_landing_turn(code):
    base = v42.RouteGateResultV42(True, "TEST1", "landing turn", qualification_state="TURN_STARTED")
    decision = hold.TerminalArrivalHoldDecision(False, code, "single gate release")
    assert not v47._can_release_expected_landing_hold(base, _assessment(), _pred(), decision)


@pytest.mark.parametrize("code", ["LTBA", "LTFM"])
@pytest.mark.parametrize("observer_position,expected_hold", [(16.0, True), (-3.0, False)])
def test_landing_path_ends_at_runway_but_preserves_pass_before_touchdown(code, observer_position, expected_hold):
    airport = global_airports.airport_for_info(route_history.AirportInfo(icao=code))
    end = airport.directed_ends[0]
    # Physically consistent 100 m/s final approach; observer is either beyond
    # the runway or underneath the remaining approach before touchdown.
    samples = [terminal.TerminalSample(
        960 + i * 10,
        *trajectory.project_point(end.latitude, end.longitude, 12 - i, end.heading_deg + 180),
        1000 - i * 30, 194.4, end.heading_deg, -3.0, 0.0,
    ) for i in range(5)]
    observer = trajectory.project_point(end.latitude, end.longitude, abs(observer_position),
                                        end.heading_deg + (180 if observer_position < 0 else 0))
    assessment = terminal.assess_terminal(samples, airport, now=1000,
                                          observer_lat=observer[0], observer_lon=observer[1])
    assert assessment.state == "ESTABLISHED_FINAL"
    prediction = SimpleNamespace(
        current_distance_km=trajectory.haversine_km(samples[-1].latitude, samples[-1].longitude, *observer),
        enters_alert_radius=True, stale=False, already_passed=False, state="Approaching",
    )
    decision = hold.evaluate_initial_terminal_hold(
        assessment, airport=airport, pred=prediction, samples=samples,
        destination=route_history.AirportInfo(icao=code), route_plausible=True,
        alert_radius_km=2.0, now=1000,
    )
    assert decision.hold is expected_hold


@pytest.mark.asyncio
@pytest.mark.parametrize("airport_code", ["LTBA", "LTFM"])
@pytest.mark.parametrize("first_stage", ["candidate", "prepare"])
async def test_monitor_rechecks_arrival_after_unsent_candidate_or_failed_delivery(monkeypatch, airport_code, first_stage):
    """Use the real v4.7 assessment, initial gate, v4.3 latch and monitor send boundary."""
    airport = global_airports.airport_for_info(route_history.AirportInfo(icao=airport_code))
    end = airport.directed_ends[0]
    samples = [terminal.TerminalSample(
        960 + i * 10,
        *trajectory.project_point(end.latitude, end.longitude, 12 - i, end.heading_deg + 180),
        1000 - i * 30, 194.4, end.heading_deg, -3.0, 0.0,
    ) for i in range(5)]
    observer = trajectory.project_point(end.latitude, end.longitude, 16, end.heading_deg)
    aircraft = SimpleNamespace(has_position=True, icao24="abc123", callsign="TEST1", aircraft_type="A320",
        latitude=samples[-1].latitude, longitude=samples[-1].longitude, heading=end.heading_deg, velocity=100.0)
    prediction = SimpleNamespace(current_distance_km=24.0, projected_closest_km=0.1, enters_alert_radius=True,
        stale=False, already_passed=False, state="Approaching", confidence="High", confidence_score=0.95,
        time_to_cpa_s=240.0, turn_rate_deg_s=0.0)
    user = {"user_id": 1, "location": {"latitude": observer[0], "longitude": observer[1], "radius_km": 2.0},
        "preferences": {"custom_aircraft": ["A320"], "selected_categories": [], "disabled_types": [],
                        "spotting": {"approach_alerts": True, "min_confidence": "Low"}}}

    class States:
        document = None

        async def find_one(self, _query):
            return None if self.document is None else dict(self.document)

        async def update_one(self, _query, update, **_kwargs):
            self.document = {**(self.document or {}), **update.get("$set", {}), "_id": "state-1"}
            for key in update.get("$unset", {}):
                self.document.pop(key, None)

    states = States()
    monkeypatch.setattr(monitor, "get_db", lambda: {"approach_states": states})
    history = [samples[-1]]
    monkeypatch.setattr(monitor, "_history", SimpleNamespace(get=lambda _icao: history))
    monkeypatch.setattr(monitor, "predict_trajectory", lambda *a, **kw: prediction)
    monkeypatch.setattr(monitor, "enqueue_snapshot", lambda **kw: None)
    monkeypatch.setattr(monitor, "enqueue_outcome", lambda **kw: None)
    monkeypatch.setattr(monitor, "_camera", AsyncMock(return_value=None))
    monkeypatch.setattr(monitor, "_environment", AsyncMock(return_value=None))
    stage = first_stage
    monkeypatch.setattr(monitor, "decide_lifecycle", lambda *a: SimpleNamespace(stage=stage))
    sender = AsyncMock(return_value=None)
    monkeypatch.setattr(monitor, "send_or_update_approach", sender)
    monkeypatch.setattr(v47.time, "time", lambda: 1000.0)
    monkeypatch.setattr(terminal, "history_for", lambda _icao: [])
    route = SimpleNamespace(destination=route_history.AirportInfo(icao=airport_code), plausible=True)
    monkeypatch.setattr(v47.v2, "_cached_route", lambda *a: (route, True))
    monkeypatch.setattr(v47.v2, "_historical_paths_cached", AsyncMock(return_value=[]))

    async def baseline(*args, **kwargs):
        # The old ensemble qualified a candidate before its first notification.
        key = v42._encounter_key(aircraft, observer[0], observer[1], 2.0)
        v42._encounters[key] = v42.EncounterState(1.0, 1.0, qualified=True)
        return v42.RouteGateResultV42(False, "TEST1", "candidate qualified", qualification_state="QUALIFIED_PASS")

    monkeypatch.setattr(v42, "evaluate_route_v42", baseline)
    monkeypatch.setattr(monitor.route_history_service, "evaluate", MethodType(v47.evaluate_route_v47, monitor.route_history_service))
    await monitor._match_user_aircraft(user, [aircraft], {})
    assert not states.document["message_id"]
    assert sender.await_count == (1 if first_stage == "prepare" else 0)
    sender.reset_mock()
    stage = "prepare"
    history[:] = samples
    await monitor._match_user_aircraft(user, [aircraft], {})
    sender.assert_not_awaited()

    # A successfully delivered message survives a restart without a shared
    # in-memory release flag. Initial scope cannot cancel the visible alert.
    states.document.update(message_id=123, active=True, updated_at=datetime.now(timezone.utc))
    v47.reset_v472_initial_hold_state_for_tests()
    sender.return_value = 123
    await monitor._match_user_aircraft(user, [aircraft], {})
    assert sender.await_count == 1
    assert sender.call_args.args[3] == "prepare"
    assert sender.call_args.args[5] == 123


@pytest.mark.asyncio
async def test_unsent_qualifications_cannot_create_cancellation_latch(monkeypatch):
    aircraft = SimpleNamespace(icao24="abc123", callsign="TEST1")
    key = v42._encounter_key(aircraft, 41.0, 29.0, 10.0)
    v42._encounters[key] = v42.EncounterState(1.0, 1.0, qualified=True)
    monkeypatch.setattr(v42, "evaluate_route_v42", AsyncMock(return_value=
        v42.RouteGateResultV42(True, "TEST1", "turn", qualification_state="TURN_STARTED")))
    for timestamp in range(1, 5):
        result = await v43.evaluate_route_v43(None, aircraft, _pred(), user_lat=41, user_lon=29,
            alert_radius_km=10, current_samples=[SimpleNamespace(timestamp=timestamp)], notification_sent=False)
        assert result.qualification_state != "CANCEL_LATCHED"
    assert not v43._states[key].latched
