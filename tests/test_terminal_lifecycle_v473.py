"""Terminal-arrival diagnostic regressions retained after route-guard consolidation.

These tests preserve runway/terminal behavior as supporting evidence only. The
canonical live route authority is app.intelligence.route_guard.
"""
from types import SimpleNamespace

import pytest

from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence import global_airports_v471 as global_airports
from app.intelligence import route_history, trajectory
from app.intelligence import terminal_arrival_hold_v472 as hold


def _assessment(airport, *, state="ESTABLISHED_FINAL", go_around=False, runway_cpa=16.0):
    return terminal.TerminalAssessment(
        airport_icao=airport.icao,
        airport_iata=airport.iata,
        terminal_area=True,
        airport_distance_km=8.0,
        state=state,
        state_confidence="High",
        confidence_penalty=0.0,
        vector_state="STABLE_STRAIGHT",
        runway_id="05",
        runway_family="05",
        runway_confidence="High",
        runway_support_count=5,
        probable_holding=False,
        holding_score=0.0,
        go_around=go_around,
        missed_approach=False,
        runway_path_cpa_km=runway_cpa,
        runway_landing_cpa_km=runway_cpa,
        reasons=(),
    )


def _pred(*, current=24.0, enters=True, stale=False, passed=False):
    return SimpleNamespace(
        current_distance_km=current,
        projected_closest_km=1.0,
        time_to_cpa_s=180.0,
        enters_alert_radius=enters,
        stale=stale,
        already_passed=passed,
        state="Passed" if passed else "Approaching",
    )


@pytest.mark.parametrize("airport_code", ["LTBA", "LTFM"])
@pytest.mark.parametrize("observer_position,expected_hold", [(16.0, True), (-3.0, False)])
def test_landing_path_ends_at_runway_but_preserves_pass_before_touchdown(airport_code, observer_position, expected_hold):
    airport = global_airports.airport_for_info(route_history.AirportInfo(icao=airport_code))
    assert airport is not None
    end = airport.directed_ends[0]
    samples = [
        terminal.TerminalSample(
            960 + i * 10,
            *trajectory.project_point(end.latitude, end.longitude, 12 - i, end.heading_deg + 180),
            1000 - i * 30,
            194.4,
            end.heading_deg,
            -3.0,
            0.0,
        )
        for i in range(5)
    ]
    observer = trajectory.project_point(
        end.latitude,
        end.longitude,
        abs(observer_position),
        end.heading_deg + (180 if observer_position < 0 else 0),
    )
    assessment = terminal.assess_terminal(
        samples,
        airport,
        now=1000,
        observer_lat=observer[0],
        observer_lon=observer[1],
    )
    assert assessment.state == "ESTABLISHED_FINAL"
    prediction = SimpleNamespace(
        current_distance_km=trajectory.haversine_km(samples[-1].latitude, samples[-1].longitude, *observer),
        enters_alert_radius=True,
        stale=False,
        already_passed=False,
        state="Approaching",
    )
    decision = hold.evaluate_initial_terminal_hold(
        assessment,
        airport=airport,
        pred=prediction,
        samples=samples,
        destination=route_history.AirportInfo(icao=airport_code),
        route_plausible=True,
        alert_radius_km=2.0,
        now=1000,
    )
    assert decision.hold is expected_hold


@pytest.mark.parametrize("airport_code", ["LTBA", "LTFM"])
def test_go_around_terminal_diagnostic_fails_open(airport_code):
    airport = global_airports.airport_for_info(route_history.AirportInfo(icao=airport_code))
    assert airport is not None
    end = airport.directed_ends[0]
    samples = [
        terminal.TerminalSample(
            960 + i * 10,
            *trajectory.project_point(end.latitude, end.longitude, 12 - i, end.heading_deg + 180),
            1000 - i * 30,
            194.4,
            end.heading_deg,
            2.5 if i == 4 else -3.0,
            0.0,
        )
        for i in range(5)
    ]
    decision = hold.evaluate_initial_terminal_hold(
        _assessment(airport, state="GO_AROUND", go_around=True),
        airport=airport,
        pred=_pred(),
        samples=samples,
        destination=route_history.AirportInfo(icao=airport_code),
        route_plausible=True,
        alert_radius_km=10.0,
        now=1000,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_GO_AROUND"


def test_stale_terminal_evidence_never_creates_authoritative_hold():
    airport = global_airports.airport_for_info(route_history.AirportInfo(icao="LTFM"))
    assert airport is not None
    end = airport.directed_ends[0]
    samples = [
        terminal.TerminalSample(
            900 + i * 10,
            *trajectory.project_point(end.latitude, end.longitude, 12 - i, end.heading_deg + 180),
            1200 - i * 30,
            194.4,
            end.heading_deg,
            -3.0,
            30.0,
        )
        for i in range(5)
    ]
    decision = hold.evaluate_initial_terminal_hold(
        _assessment(airport),
        airport=airport,
        pred=_pred(stale=True),
        samples=samples,
        destination=route_history.AirportInfo(icao="LTFM"),
        route_plausible=True,
        alert_radius_km=10.0,
        now=1000,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_STALE"
