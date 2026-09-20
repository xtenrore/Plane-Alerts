from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from app.intelligence import airport_terminal_v47 as v47


@pytest.fixture(autouse=True)
def _reset():
    v47.reset_v47_state_for_tests()
    yield
    v47.reset_v47_state_for_tests()


def _airport(*, parallel: bool = False, crossing: bool = False) -> v47.AirportGeometry:
    runways = [
        v47.RunwayGeometry(
            "TEST",
            v47.RunwayEnd("18", 0.050, 0.000, 180.0),
            v47.RunwayEnd("36", 0.000, 0.000, 0.0),
        )
    ]
    if parallel:
        runways.append(v47.RunwayGeometry(
            "TEST",
            v47.RunwayEnd("18R", 0.050, 0.010, 180.0),
            v47.RunwayEnd("36L", 0.000, 0.010, 0.0),
        ))
    if crossing:
        runways.append(v47.RunwayGeometry(
            "TEST",
            v47.RunwayEnd("09", 0.025, -0.025, 90.0),
            v47.RunwayEnd("27", 0.025, 0.025, 270.0),
        ))
    return v47.AirportGeometry("TEST", "TST", "Synthetic Airport", 0.025, 0.0, 50.0, tuple(runways))


def _s(t, lat, lon, heading, *, alt=1200.0, vr=-2.0, speed=180.0):
    return v47.TerminalSample(float(t), float(lat), float(lon), alt, speed, heading, vr, 0.0)


def _prediction(*, current=30.0, cpa=4.0, eta=240.0, stale=False, enters=True):
    return SimpleNamespace(
        current_distance_km=current,
        projected_closest_km=cpa,
        time_to_cpa_s=eta,
        stale=stale,
        enters_alert_radius=enters,
    )


def test_terminal_area_entry_reduces_shadow_confidence_without_hard_veto():
    airport = _airport()
    samples = [_s(0, 0.30, 0.0, 180), _s(10, 0.26, 0.0, 180), _s(20, 0.22, 0.0, 180), _s(30, 0.18, 0.0, 180)]
    result = v47.assess_terminal(samples, airport, now=30)
    assert result.terminal_area is True
    assert 0.0 < result.confidence_penalty <= 0.18
    hold, _ = v47.shadow_terminal_hold(result, pred=_prediction(current=8.0), alert_radius_km=10.0)
    assert hold is False


def test_sustained_base_turn_requires_multiple_consistent_samples():
    airport = _airport()
    # A coherent descending arc toward runway 18: the position track follows the
    # same sustained right turn described by the heading samples instead of
    # moving straight while headings rotate independently.
    samples = [
        _s(0, 0.130, -0.080, 100, alt=1700),
        _s(10, 0.126, -0.055, 110, alt=1630),
        _s(20, 0.118, -0.032, 120, alt=1560),
        _s(30, 0.107, -0.014, 130, alt=1490),
        _s(40, 0.092, -0.002, 140, alt=1420),
        _s(50, 0.075, 0.004, 150, alt=1350),
    ]
    result = v47.assess_terminal(samples, airport, now=50)
    assert result.vector_state == "SUSTAINED_TURN"
    assert result.state in {"DOWNWIND_TO_BASE", "BASE_TURN"}


def test_single_noisy_heading_correction_is_not_a_sustained_turn():
    airport = _airport()
    headings = [90, 91, 89, 112, 90, 91]
    samples = [_s(i * 5, 0.20, 0.10 - i * 0.005, heading, vr=-0.3) for i, heading in enumerate(headings)]
    result = v47.assess_terminal(samples, airport, now=25)
    assert result.vector_state != "SUSTAINED_TURN"
    assert result.state not in {"BASE_TURN", "DOWNWIND_TO_BASE"}


def test_final_intercept_and_established_final_are_runway_relative():
    airport = _airport()
    intercept = [
        _s(0, 0.16, 0.035, 145),
        _s(10, 0.14, 0.026, 155),
        _s(20, 0.12, 0.016, 165),
        _s(30, 0.10, 0.006, 174),
    ]
    intercept_result = v47.assess_terminal(intercept, airport, now=30)
    assert intercept_result.state == "FINAL_INTERCEPT"

    final = [
        _s(0, 0.15, 0.000, 178),
        _s(10, 0.13, 0.000, 179),
        _s(20, 0.11, 0.000, 181),
        _s(30, 0.09, 0.000, 180),
        _s(40, 0.07, 0.000, 180),
    ]
    final_result = v47.assess_terminal(final, airport, now=40)
    assert final_result.state == "ESTABLISHED_FINAL"
    assert final_result.runway_id == "18"


def test_sustained_vector_is_distinct_from_one_sample_noise():
    airport = _airport()
    samples = [_s(i * 10, 0.45 - i * 0.01, 0.15, heading, vr=0.0) for i, heading in enumerate([210, 212, 214, 216, 216, 216])]
    result = v47.assess_terminal(samples, airport, now=50)
    assert result.vector_state in {"SUSTAINED_VECTOR", "VARIABLE_TRACK", "STABLE_STRAIGHT"}
    assert result.vector_state != "SUSTAINED_TURN"


def test_probable_holding_requires_loop_time_geographic_repeat_and_altitude_band():
    airport = _airport()
    samples = []
    for i in range(30):
        angle = math.radians((i * 30) % 360)
        samples.append(_s(i * 10, 0.30 + 0.045 * math.cos(angle), 0.045 * math.sin(angle), (i * 30 + 90) % 360, alt=2400 + (i % 3) * 20, vr=0.0, speed=190))
    result = v47.assess_terminal(samples, airport, now=290)
    assert result.probable_holding is True
    assert result.state == "PROBABLE_HOLDING"
    assert result.holding_score >= 0.75
    hold, reason = v47.shadow_terminal_hold(result, pred=_prediction(current=25, eta=300), alert_radius_km=10)
    assert hold is True
    assert "holding" in reason


def test_go_around_and_missed_approach_replaces_old_landing_expectation_state():
    airport = _airport()
    samples = [
        _s(0, 0.13, 0.000, 180, alt=900, vr=-3),
        _s(10, 0.11, 0.000, 180, alt=750, vr=-3),
        _s(20, 0.09, 0.000, 180, alt=600, vr=-3),
        _s(30, 0.065, 0.000, 180, alt=430, vr=-3),
        _s(40, 0.053, 0.000, 180, alt=320, vr=-2),
        _s(50, 0.049, 0.002, 195, alt=390, vr=2.5),
        _s(60, 0.045, 0.006, 210, alt=500, vr=4.0),
    ]
    result = v47.assess_terminal(samples, airport, now=60)
    assert result.go_around is True
    assert result.missed_approach is True
    assert result.state == "MISSED_APPROACH"
    hold, _ = v47.shadow_terminal_hold(result, pred=_prediction(), alert_radius_km=10)
    assert hold is False


def _ac(icao, now, lat, lon, heading, *, vr=-2.0, alt=700.0):
    return SimpleNamespace(
        icao24=icao,
        latitude=lat,
        longitude=lon,
        altitude=alt,
        ground_speed=180.0,
        velocity=None,
        heading=heading,
        vertical_rate_mps=vr,
        position_age_s=0.0,
        observed_at=now,
        timestamp=now,
    )


def test_runway_inference_requires_multiple_aircraft_and_exposes_evidence_count():
    airport = v47.register_airport(_airport())
    now = 1000.0
    v47.enqueue_provider_snapshot([_ac("a1", now, 0.12, 0, 180)], now=now)
    first = v47.infer_runway_configuration(airport, now=now)
    assert first.confidence == "Uncertain"
    assert first.runway_family == ""

    v47.enqueue_provider_snapshot([
        _ac("a2", now + 5, 0.13, 0, 180),
        _ac("a3", now + 5, 0.14, 0, 180),
    ], now=now + 5)
    inferred = v47.infer_runway_configuration(airport, now=now + 5)
    assert inferred.runway_family == "18"
    assert inferred.supporting_aircraft == 3
    assert inferred.confidence in {"Medium", "High"}


def test_runway_configuration_change_needs_new_multi_aircraft_evidence():
    airport = v47.register_airport(_airport())
    now = 1000.0
    v47.enqueue_provider_snapshot([_ac(f"s{i}", now + i, 0.13 + i * .002, 0, 180) for i in range(3)], now=now + 3)
    south = v47.infer_runway_configuration(airport, now=now + 3)
    assert south.runway_family == "18"

    later = now + 900
    v47.enqueue_provider_snapshot([_ac(f"n{i}", later + i, -0.08 - i * .002, 0, 0) for i in range(4)], now=later + 4)
    north = v47.infer_runway_configuration(airport, now=later + 4)
    assert north.runway_family == "36"
    assert north.changed is True


def test_parallel_and_crossing_runways_use_generic_geometry_not_ist_headings():
    airport = v47.register_airport(_airport(parallel=True, crossing=True))
    assert len(airport.runways) == 3
    now = 2000.0
    v47.enqueue_provider_snapshot([
        _ac("p1", now, 0.12, 0.010, 180),
        _ac("p2", now + 1, 0.13, 0.010, 180),
        _ac("p3", now + 2, 0.14, 0.010, 180),
    ], now=now + 2)
    inferred = v47.infer_runway_configuration(airport, now=now + 2)
    assert inferred.runway_family == "18"
    assert inferred.supporting_aircraft == 3


def test_recent_movement_clusters_are_bounded_decayed_and_sample_counted():
    airport = v47.register_airport(_airport())
    now = 3000.0
    v47.enqueue_provider_snapshot([
        _ac("a1", now, 0.12, 0, 180),
        _ac("a2", now, 0.13, 0, 180),
        _ac("t1", now, 0.20, 0.20, 90, vr=0, alt=7000),
    ], now=now)
    rows = v47.recent_movement_clusters(airport, now=now)
    labels = {row["label"] for row in rows}
    assert any(label.startswith("arrival-") for label in labels)
    assert "overhead-transit" in labels
    assert max(row["sample_count"] for row in rows) <= 2
    later = v47.recent_movement_clusters(airport, now=now + 1000)
    assert all(row["decayed_support"] < row["sample_count"] for row in later)
