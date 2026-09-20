from __future__ import annotations

from types import SimpleNamespace

from app.intelligence import airport_terminal_v47 as airport_v47
from app.intelligence import route_guard_v42 as v42
from app.intelligence import route_guard_v47 as v47


def _base(state: str, *, suppress: bool = True):
    return v42.RouteGateResultV42(
        suppress_alert=suppress,
        callsign="THY1",
        reason="baseline",
        qualification_state=state,
    )


def _assessment(*, go=False, missed=False):
    return airport_v47.TerminalAssessment(
        airport_icao="LTFM",
        airport_iata="IST",
        terminal_area=True,
        airport_distance_km=15.0,
        state="MISSED_APPROACH" if missed else "GO_AROUND" if go else "TERMINAL_UNCERTAIN",
        state_confidence="High" if (go or missed) else "Low",
        confidence_penalty=0.18 if (go or missed) else 0.06,
        vector_state="SUSTAINED_TURN",
        runway_id="34R",
        runway_family="34",
        runway_confidence="Medium",
        runway_support_count=5,
        probable_holding=False,
        holding_score=0.0,
        go_around=go,
        missed_approach=missed,
        runway_path_cpa_km=30.0,
        reasons=(),
    )


def _pred(*, stale=False, enters=True, current=20.0):
    return SimpleNamespace(stale=stale, enters_alert_radius=enters, current_distance_km=current)


def test_go_around_can_only_release_expected_landing_turn_hold():
    assert v47._can_release_expected_landing_hold(_base("EXPECTED_TURN_PENDING"), _assessment(go=True), _pred())
    assert v47._can_release_expected_landing_hold(_base("TURN_STARTED"), _assessment(missed=True), _pred())
    assert not v47._can_release_expected_landing_hold(_base("CANCEL_LATCHED"), _assessment(go=True), _pred())
    assert not v47._can_release_expected_landing_hold(_base("SHADOW_ROUTE_EVIDENCE"), _assessment(go=True), _pred())


def test_go_around_fail_safe_requires_fresh_live_geometry_that_still_enters_radius():
    base = _base("EXPECTED_TURN_PENDING")
    assessment = _assessment(go=True)
    assert not v47._can_release_expected_landing_hold(base, assessment, _pred(stale=True))
    assert not v47._can_release_expected_landing_hold(base, assessment, _pred(enters=False))


def test_runway_and_holding_candidate_is_shadow_only_and_never_overrides_physical_entry():
    final = airport_v47.TerminalAssessment(
        airport_icao="TEST", airport_iata="TST", terminal_area=True, airport_distance_km=20,
        state="ESTABLISHED_FINAL", state_confidence="High", confidence_penalty=0.06,
        vector_state="STABLE_STRAIGHT", runway_id="18", runway_family="18", runway_confidence="Medium",
        runway_support_count=4, probable_holding=False, holding_score=0.0, go_around=False,
        missed_approach=False, runway_path_cpa_km=35.0, reasons=(),
    )
    hold, _ = airport_v47.shadow_terminal_hold(
        final,
        pred=SimpleNamespace(stale=False, current_distance_km=30.0, time_to_cpa_s=180.0),
        alert_radius_km=10.0,
    )
    assert hold is True
    inside, reason = airport_v47.shadow_terminal_hold(
        final,
        pred=SimpleNamespace(stale=False, current_distance_km=8.0, time_to_cpa_s=60.0),
        alert_radius_km=10.0,
    )
    assert inside is False
    assert "physical" in reason
