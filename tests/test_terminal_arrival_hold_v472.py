from __future__ import annotations

from types import SimpleNamespace

from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence import global_airports_v471 as global_airports
from app.intelligence import route_history as route_mod
from app.intelligence import terminal_arrival_hold_v472 as hold_v472
from app.intelligence import trajectory as traj
from app.intelligence.airport_repository import airport_repository

NOW = 1_000.0


def _ltba_geometry() -> terminal.AirportGeometry:
    info = route_mod.AirportInfo(icao="LTBA", iata="ISL", name="Istanbul Ataturk Airport")
    airport = global_airports.airport_for_info(info)
    assert airport is not None
    return airport


def _arrival_samples(airport: terminal.AirportGeometry) -> list[terminal.TerminalSample]:
    samples: list[terminal.TerminalSample] = []
    for timestamp, distance_km, altitude_m in zip(
        (960.0, 970.0, 980.0, 990.0, 1000.0),
        (32.0, 27.0, 22.0, 17.0, 13.0),
        (2700.0, 2450.0, 2200.0, 1950.0, 1700.0),
    ):
        lat, lon = traj.project_point(airport.latitude, airport.longitude, distance_km, 230.0)
        samples.append(
            terminal.TerminalSample(
                timestamp=timestamp,
                latitude=lat,
                longitude=lon,
                altitude_m=altitude_m,
                speed_kts=210.0,
                heading_deg=50.0,
                vertical_rate_mps=-3.0,
                position_age_s=0.0,
            )
        )
    return samples


def _assessment(
    airport: terminal.AirportGeometry,
    *,
    state: str = "TERMINAL_UNCERTAIN",
    go_around: bool = False,
    missed_approach: bool = False,
    runway_path_cpa_km: float | None = None,
) -> terminal.TerminalAssessment:
    return terminal.TerminalAssessment(
        airport_icao=airport.icao,
        airport_iata=airport.iata,
        terminal_area=True,
        airport_distance_km=13.0,
        state=state,
        state_confidence="High" if state == "ESTABLISHED_FINAL" else "Low",
        confidence_penalty=0.06,
        vector_state="STABLE_STRAIGHT",
        runway_id="05" if state == "ESTABLISHED_FINAL" else "",
        runway_family="05" if state == "ESTABLISHED_FINAL" else "",
        runway_confidence="Medium" if state == "ESTABLISHED_FINAL" else "Uncertain",
        runway_support_count=4 if state == "ESTABLISHED_FINAL" else 0,
        probable_holding=False,
        holding_score=0.0,
        go_around=go_around,
        missed_approach=missed_approach,
        runway_path_cpa_km=runway_path_cpa_km,
        runway_landing_cpa_km=runway_path_cpa_km,
        reasons=(),
    )


def _pred(*, current=24.0, cpa=4.0, enters=True, stale=False, passed=False):
    return SimpleNamespace(
        current_distance_km=current,
        projected_closest_km=cpa,
        time_to_cpa_s=150.0,
        enters_alert_radius=enters,
        stale=stale,
        already_passed=passed,
        state="Passed" if passed else "Approaching",
    )


def _destination(airport: terminal.AirportGeometry, *, icao="LTBA", iata="ISL"):
    return route_mod.AirportInfo(
        icao=icao,
        iata=iata,
        name=airport.name,
        latitude=airport.latitude,
        longitude=airport.longitude,
    )


def test_compiled_database_resolves_ltba_and_isl_to_same_active_airport_with_runway_geometry():
    assert airport_repository.available
    by_icao = airport_repository.by_code("LTBA")
    by_iata = airport_repository.by_code("ISL")
    assert by_icao is not None and by_iata is not None
    assert by_icao.ident == by_iata.ident == "LTBA"
    assert by_icao.iata_code == by_iata.iata_code == "ISL"
    assert by_icao.closed is False
    runway_pairs = {(row.le_ident, row.he_ident) for row in by_icao.runways}
    assert ("05", "23") in runway_pairs

    from_iata_only = global_airports.airport_for_info(
        route_mod.AirportInfo(iata="ISL", name="Istanbul Ataturk Airport")
    )
    assert from_iata_only is not None
    assert from_iata_only.icao == "LTBA"
    assert from_iata_only.iata == "ISL"
    assert {(r.end_a.identifier, r.end_b.identifier) for r in from_iata_only.runways} >= {("05", "23")}


def test_error_museum_ltba_low_slow_descent_false_straight_line_cpa_is_held_until_landing_turn_removes_pass():
    airport = _ltba_geometry()
    samples = _arrival_samples(airport)
    assessment = _assessment(airport)

    decision = hold_v472.evaluate_initial_terminal_hold(
        assessment,
        airport=airport,
        pred=_pred(current=24.0, cpa=4.0, enters=True),
        samples=samples,
        destination=_destination(airport, iata="ISL"),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.82,
        baseline_expected_turn_state="EXPECTED_TURN_PENDING",
        now=NOW,
    )
    assert decision.hold is True
    assert decision.code == "HOLD_STRONG_TERMINAL_ARRIVAL"
    assert decision.destination_match is True
    assert decision.airport_trend_km_s is not None and decision.airport_trend_km_s < -0.008

    after_turn = hold_v472.evaluate_initial_terminal_hold(
        assessment,
        airport=airport,
        pred=_pred(current=21.0, cpa=18.0, enters=False),
        samples=samples,
        destination=_destination(airport, iata="ISL"),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.82,
        baseline_expected_turn_state="TURN_STARTED",
        now=NOW,
    )
    assert after_turn.hold is False
    assert after_turn.code == "NO_LIVE_PASS"


def test_destination_isl_by_itself_never_suppresses_alert():
    airport = _ltba_geometry()
    samples = [
        terminal.TerminalSample(
            s.timestamp,
            s.latitude,
            s.longitude,
            7800.0,
            430.0,
            s.heading_deg,
            0.0,
            0.0,
        )
        for s in _arrival_samples(airport)
    ]
    decision = hold_v472.evaluate_initial_terminal_hold(
        _assessment(airport),
        airport=airport,
        pred=_pred(),
        samples=samples,
        destination=_destination(airport, icao="", iata="ISL"),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.9,
        baseline_expected_turn_state="EXPECTED_TURN_PENDING",
        now=NOW,
    )
    assert decision.hold is False
    assert decision.destination_match is True
    assert decision.code in {"TERMINAL_EVIDENCE_BELOW_THRESHOLD", "TERMINAL_SCORE_BELOW_THRESHOLD"}


def test_genuine_physical_radius_entry_releases_terminal_arrival_hold():
    airport = _ltba_geometry()
    decision = hold_v472.evaluate_initial_terminal_hold(
        _assessment(airport),
        airport=airport,
        pred=_pred(current=8.0, cpa=2.0, enters=True),
        samples=_arrival_samples(airport),
        destination=_destination(airport),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.9,
        baseline_expected_turn_state="EXPECTED_TURN_PENDING",
        now=NOW,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_OBSERVED_INSIDE"


def test_go_around_or_missed_approach_releases_terminal_arrival_hold():
    airport = _ltba_geometry()
    decision = hold_v472.evaluate_initial_terminal_hold(
        _assessment(airport, state="GO_AROUND", go_around=True),
        airport=airport,
        pred=_pred(),
        samples=_arrival_samples(airport),
        destination=_destination(airport),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.9,
        baseline_expected_turn_state="TURN_STARTED",
        now=NOW,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_GO_AROUND"


def test_expected_turn_that_does_not_occur_releases_hold_back_to_live_geometry():
    airport = _ltba_geometry()
    decision = hold_v472.evaluate_initial_terminal_hold(
        _assessment(airport),
        airport=airport,
        pred=_pred(),
        samples=_arrival_samples(airport),
        destination=_destination(airport),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.9,
        baseline_expected_turn_state="TURN_DID_NOT_OCCUR",
        now=NOW,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_TRAJECTORY_CHANGED"


def test_runway_aligned_path_that_can_hit_observer_is_never_suppressed():
    airport = _ltba_geometry()
    decision = hold_v472.evaluate_initial_terminal_hold(
        _assessment(airport, state="ESTABLISHED_FINAL", runway_path_cpa_km=5.0),
        airport=airport,
        pred=_pred(),
        samples=_arrival_samples(airport),
        destination=_destination(airport),
        route_plausible=True,
        alert_radius_km=10.0,
        baseline_terminal_score=0.9,
        baseline_expected_turn_state="EXPECTED_TURN_PENDING",
        now=NOW,
    )
    assert decision.hold is False
    assert decision.code == "RELEASE_RUNWAY_PATH_CAN_PASS"
