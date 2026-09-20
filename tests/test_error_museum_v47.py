from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.intelligence import airport_terminal_v47 as v47


@pytest.fixture(autouse=True)
def _reset():
    v47.reset_v47_state_for_tests()
    yield
    v47.reset_v47_state_for_tests()


def _s(t, lat, lon, heading, alt, vr, speed=170):
    return v47.TerminalSample(t, lat, lon, alt, speed, heading, vr, 0.0)


def _pred(current, cpa, eta, *, stale=False):
    return SimpleNamespace(current_distance_km=current, projected_closest_km=cpa, time_to_cpa_s=eta, stale=stale, enters_alert_radius=cpa <= 10)


def test_ist_normal_34r_arrival_can_be_shadow_held_when_runway_path_misses_observer():
    samples = [
        _s(0, 41.20, 28.7099, 354, 1800, -3),
        _s(10, 41.22, 28.7099, 354, 1600, -3),
        _s(20, 41.24, 28.7099, 354, 1400, -3),
        _s(30, 41.25, 28.7099, 354, 1200, -3),
    ]
    assessment = v47.assess_terminal(samples, v47.LTFM, now=30, observer_lat=41.25, observer_lon=28.56)
    assert assessment.terminal_area
    assert assessment.state == "ESTABLISHED_FINAL"
    hold, _ = v47.shadow_terminal_hold(assessment, pred=_pred(25, 4, 200), alert_radius_km=10)
    assert hold is True


def test_ist_genuine_overhead_pass_is_never_erased_by_runway_expectation():
    samples = [
        _s(0, 41.30, 28.60, 90, 4200, 0),
        _s(10, 41.30, 28.64, 90, 4200, 0),
        _s(20, 41.30, 28.68, 90, 4200, 0),
        _s(30, 41.30, 28.72, 90, 4200, 0),
    ]
    assessment = v47.assess_terminal(samples, v47.LTFM, now=30, observer_lat=41.30, observer_lon=28.73)
    hold, reason = v47.shadow_terminal_hold(assessment, pred=_pred(0.8, 0.2, 10), alert_radius_km=10)
    assert hold is False
    assert "physical" in reason


def test_ist_go_around_switches_from_final_to_missed_approach_and_shadow_releases():
    samples = [
        _s(0, 41.205, 28.7099, 354, 1000, -3),
        _s(10, 41.225, 28.7099, 354, 800, -3),
        _s(20, 41.245, 28.7099, 354, 600, -3),
        _s(30, 41.258, 28.7099, 354, 420, -2),
        _s(40, 41.263, 28.7100, 354, 300, -1.5),
        _s(50, 41.270, 28.7140, 10, 390, 2.5),
        _s(60, 41.279, 28.7220, 24, 520, 4.0),
    ]
    assessment = v47.assess_terminal(samples, v47.LTFM, now=60, observer_lat=41.31, observer_lon=28.74)
    assert assessment.go_around is True
    assert assessment.missed_approach is True
    hold, _ = v47.shadow_terminal_hold(assessment, pred=_pred(18, 5, 120), alert_radius_km=10)
    assert hold is False


def test_stale_adsb_during_approach_never_creates_new_airport_shadow_suppression_decision():
    samples = [
        _s(0, 41.20, 28.7099, 354, 1800, -3),
        _s(10, 41.22, 28.7099, 354, 1600, -3),
        _s(20, 41.24, 28.7099, 354, 1400, -3),
        _s(30, 41.25, 28.7099, 354, 1200, -3),
    ]
    assessment = v47.assess_terminal(samples, v47.LTFM, now=30, observer_lat=41.25, observer_lon=28.56)
    hold, reason = v47.shadow_terminal_hold(assessment, pred=_pred(25, 4, 200, stale=True), alert_radius_km=10)
    assert hold is False
    assert "stale" in reason


def test_missing_destination_does_not_block_generic_terminal_or_transit_classification():
    samples = [
        _s(0, 41.15, 28.60, 40, 6000, 0, 300),
        _s(10, 41.17, 28.62, 40, 6000, 0, 300),
        _s(20, 41.19, 28.64, 40, 6000, 0, 300),
        _s(30, 41.21, 28.66, 40, 6000, 0, 300),
    ]
    assessment = v47.assess_terminal(samples, v47.LTFM, now=30)
    assert assessment.airport_icao == "LTFM"
    assert assessment.state not in {"ESTABLISHED_FINAL", "FINAL_INTERCEPT"}


def test_multiple_airport_layouts_are_handled_by_same_classifier():
    parallel = v47.AirportGeometry(
        "PARA", "PRA", "Parallel", 10.0, 10.0, 20,
        (
            v47.RunwayGeometry("PARA", v47.RunwayEnd("09L", 10.0, 9.98, 90), v47.RunwayEnd("27R", 10.0, 10.02, 270)),
            v47.RunwayGeometry("PARA", v47.RunwayEnd("09R", 10.01, 9.98, 90), v47.RunwayEnd("27L", 10.01, 10.02, 270)),
        ),
    )
    single = v47.AirportGeometry(
        "SING", "SNG", "Single", 20.0, 20.0, 100,
        (v47.RunwayGeometry("SING", v47.RunwayEnd("08", 20.0, 19.98, 80), v47.RunwayEnd("26", 20.0, 20.02, 260)),),
    )
    crossing = v47.AirportGeometry(
        "CROS", "CRS", "Crossing", 30.0, 30.0, 50,
        (
            v47.RunwayGeometry("CROS", v47.RunwayEnd("18", 30.02, 30.0, 180), v47.RunwayEnd("36", 29.98, 30.0, 0)),
            v47.RunwayGeometry("CROS", v47.RunwayEnd("09", 30.0, 29.98, 90), v47.RunwayEnd("27", 30.0, 30.02, 270)),
        ),
    )
    for airport in (parallel, single, crossing):
        v47.register_airport(airport)
        assert v47.nearest_airport(airport.latitude, airport.longitude, max_distance_km=1.0) == airport
        result = v47.assess_terminal([
            _s(0, airport.latitude + .1, airport.longitude, 180, 1400, -2),
            _s(10, airport.latitude + .08, airport.longitude, 180, 1200, -2),
            _s(20, airport.latitude + .06, airport.longitude, 180, 1000, -2),
            _s(30, airport.latitude + .04, airport.longitude, 180, 800, -2),
        ], airport, now=30)
        assert result.airport_icao == airport.icao
        assert result.terminal_area is True
