"""Plane Alerts v6.0.0 — Main-Based Diagnostic and Fix Plan Verification Suite.

Covers:
- Phase 0: Main baseline pinning, commit history review, secret-redacted restorable backup,
  and confirmed rollback target readiness (v5.8.8 / community stable v5.5.3).
- Phase 1: Pure diagnostic capture for a real notification and a real suppression with
  zero prediction behavior change, coordinate/credential privacy redaction, bounded
  retention, clean disable toggle, unknown coverage for missing/stale ADS-B data,
  and structured 5-way user feedback.
- Phase 2: Fresh failure triage into the 5 problem classes (`DATA_FRESHNESS`,
  `ETA_TRAJECTORY`, `AIRPORT_LOGIC`, `TERRAIN_VISIBILITY`, `NOTIFICATION_LIFECYCLE`)
  without treating inherited Error Museum records as ground truth.
- Phase 3A: Explainable airport/arrival detection against airport boundaries, runway
  thresholds, centerlines, altitude AGL, heading, and track motion (`ARRIVAL`,
  `DEPARTURE`, `OVERFLIGHT`, `HOLDING`, `MISSED_APPROACH`, `AIRPORT_NEARBY`).
- Phase 3B: Terrain line-of-sight visibility with 4/3 Earth-radius curvature drop,
  strict separation from weather/buildings, and `unknown` for missing/coarse/sparse DEM.
- Phase 3C: ETA/CPA root-cause attribution, sub-step radius entry interpolation,
  preservation of `PREDICTION_VERSION == "5.3-3d-proximity-age-aware"`, and ETA error
  scoring strictly on confirmed observed passes.
- Phase 4: Release validation measures with explicit denominators and unknown-coverage shares.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import List, Optional

from app.intelligence.airport_arrival_v6 import (
    evaluate_airport_arrival_v6,
    match_runway_threshold,
)
from app.intelligence.airport_terminal_v47 import (
    AirportGeometry,
    RunwayEnd,
    RunwayGeometry,
    TerminalSample,
)
from app.intelligence.diagnostics_v6 import (
    DEFAULT_PINNED_MAIN_SHA,
    DiagnosticCollectorV6,
    USER_FEEDBACK_CATEGORIES_V6,
    V6_FEEDBACK_LABELS,
    capture_phase0_baseline_snapshot,
    classify_observation_freshness,
    compute_v6_success_measures,
    normalize_v6_feedback_reason,
    triage_fresh_case,
    verify_rollback_readiness,
)
from app.intelligence.eta_cpa_v6 import (
    diagnose_eta_cpa,
    evaluate_confirmed_pass_eta_error,
    refine_age_aware_radius_entry_s,
)
from app.intelligence.terrain_visibility_v6 import (
    TerrainProfileData,
    TerrainProfilePoint,
    earth_curvature_drop_m,
    evaluate_terrain_line_of_sight,
)
from app.intelligence.trajectory import HistorySample
from app.version import PREDICTION_VERSION, VERSION


@dataclass
class _DummyPathPoint:
    seconds: float
    latitude: float
    longitude: float
    horizontal_km: float


@dataclass
class _DummyPrediction:
    time_to_cpa_s: float = 120.0
    radius_entry_s: Optional[float] = 90.0
    closest_distance_km: float = 1.8
    current_distance_km: float = 14.0
    turn_rate_deg_s: float = 0.0
    acceleration_kts_s: float = 0.0
    heading_stability_deg: float = 2.0
    speed_stability_kts: float = 4.0
    stale: bool = False
    confidence: str = "High"
    path: List[_DummyPathPoint] = field(default_factory=list)


@dataclass
class _DummyAircraft:
    icao24: str = "4BB123"
    callsign: str = "THY6AB"
    latitude: float = 41.3400
    longitude: float = 28.7410
    altitude: float = 550.0  # meters
    ground_speed: float = 145.0  # knots
    heading: float = 180.0
    vertical_rate_mps: float = -3.8
    position_age_s: float = 2.1


@dataclass
class _DummyDestination:
    icao: str = "LTFM"
    iata: str = "IST"
    code: str = "LTFM"


def _sample_ltfm_geometry() -> AirportGeometry:
    end_17l = RunwayEnd("17L", 41.2930, 28.7410, 180.0)
    end_35r = RunwayEnd("35R", 41.2560, 28.7410, 360.0)
    rw = RunwayGeometry(airport_icao="LTFM", end_a=end_17l, end_b=end_35r)
    return AirportGeometry(
        icao="LTFM",
        iata="IST",
        name="Istanbul Airport",
        latitude=41.275278,
        longitude=28.751944,
        elevation_m=99.0,
        runways=(rw,),
    )


def test_phase0_main_pin_backup_and_rollback_readiness() -> None:
    assert VERSION == "6.0.0"
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"
    raw_cfg = {
        "DATABASE_BACKEND": "sqlite",
        "ALERT_RADIUS_KM": 8.0,
        "TELEGRAM_BOT_TOKEN": "123456:secret_token_value",
        "OBSERVER_LAT": 41.0422,
        "OBSERVER_LON": 28.9861,
        "MONGO_URI": "mongodb+srv://user:secret@cluster.local/db",
    }
    pin = capture_phase0_baseline_snapshot(
        pinned_main_sha=DEFAULT_PINNED_MAIN_SHA,
        raw_config=raw_cfg,
    )
    assert pin.pinned_main_sha == DEFAULT_PINNED_MAIN_SHA
    assert pin.baseline_version == "5.8.8"
    assert pin.target_version == "6.0.0"
    assert pin.rollback_target_release == "v5.8.8"
    assert pin.community_rollback_release == "v5.5.3"
    assert pin.inherited_artifacts_unverified is True
    assert pin.restorable_config_backup["TELEGRAM_BOT_TOKEN"] == "<REDACTED>"
    assert pin.restorable_config_backup["OBSERVER_LAT"] == "<REDACTED>"
    assert pin.restorable_config_backup["OBSERVER_LON"] == "<REDACTED>"

    readiness = verify_rollback_readiness(pin)
    assert readiness["rollback_ready"] is True
    assert readiness["secrets_redacted"] is True


def test_phase1_diagnostic_capture_no_behavior_change_and_privacy() -> None:
    collector = DiagnosticCollectorV6(enabled=True, max_records=32)
    airport = _sample_ltfm_geometry()

    # Case 1: Real notification (genuine low pass over observer)
    ac_alert = _DummyAircraft(
        icao24="4BB001",
        callsign="THY101",
        latitude=41.0500,
        longitude=28.9800,
        altitude=1050.0,
        ground_speed=210.0,
        heading=90.0,
        vertical_rate_mps=0.0,
    )
    terrain_clear = evaluate_terrain_line_of_sight(
        observer_lat=41.0422,
        observer_lon=28.9861,
        observer_elevation_m=85.0,
        aircraft_lat=ac_alert.latitude,
        aircraft_lon=ac_alert.longitude,
        aircraft_altitude_m=1050.0,
        profile=TerrainProfileData(
            points=(
                TerrainProfilePoint(distance_km=0.3, elevation_m=90.0),
                TerrainProfilePoint(distance_km=0.7, elevation_m=95.0),
            ),
            data_version="SRTM_30M_V6",
            quality="verified",
            resolution_m=30.0,
            source="srtm30m",
        ),
    )
    assert terrain_clear.status == "clear"

    samples = [
        HistorySample(1000.0, 41.0500, 28.9600, 1050.0, 210.0, 90.0, 0.0, 1.5),
        HistorySample(1005.0, 41.0500, 28.9700, 1050.0, 210.0, 90.0, 0.0, 1.8),
        HistorySample(1010.0, 41.0500, 28.9800, 1050.0, 210.0, 90.0, 0.0, 2.1),
    ]
    pred_alert = _DummyPrediction(time_to_cpa_s=68.0, radius_entry_s=40.0, closest_distance_km=1.45)
    eta_diag = diagnose_eta_cpa(
        prediction=pred_alert,
        samples=samples,
        alert_radius_km=8.0,
        now=1012.0,
    )
    assert eta_diag.primary_driver == "STABLE_NOMINAL"

    rec_alert = collector.capture(
        icao=ac_alert.icao24,
        callsign=ac_alert.callsign,
        observer_lat=41.0422,
        observer_lon=28.9861,
        observer_id="user_42",
        observation_timestamp_s=1010.0,
        observation_source="airplanes.live",
        observation_age_s=2.1,
        relative_distance_km=2.8,
        relative_bearing_deg=14.0,
        altitude_ft=3445.0,
        vertical_rate_fpm=0.0,
        ground_speed_kt=ac_alert.ground_speed,
        heading_deg=ac_alert.heading,
        track_history_points=3,
        predicted_cpa_km=1.45,
        predicted_cpa_3d_km=1.78,
        predicted_eta_s=68.0,
        confidence=0.92,
        uncertainty_km=0.35,
        terrain_eval=terrain_clear,
        eta_cpa_eval=eta_diag,
        final_decision="alert",
        stable_reason_code="QUALIFIED_IN_RADIUS_PASS",
    )
    assert rec_alert is not None
    assert rec_alert.final_decision == "alert"
    assert rec_alert.terrain_los_status == "clear"
    assert rec_alert.coverage_status == "confirmed"
    serialized = str(rec_alert.to_dict())
    assert "41.0422" not in serialized
    assert "28.9861" not in serialized
    assert rec_alert.observer_ref.startswith("obs_")

    # Case 2: Real suppression (final approach into LTFM Runway 17L before reaching observer south of airport)
    ac_arr = _DummyAircraft(
        icao24="4BB002",
        callsign="THY202",
        latitude=41.3350,
        longitude=28.7410,
        altitude=520.0,
        ground_speed=142.0,
        heading=180.0,
        vertical_rate_mps=-3.8,
    )
    arr_samples = [
        TerminalSample(1000.0, 41.3500, 28.7410, 620.0, 145.0, 180.0, -3.6, 1.0),
        TerminalSample(1010.0, 41.3350, 28.7410, 520.0, 142.0, 180.0, -3.8, 1.2),
    ]
    pred_arr = _DummyPrediction(
        time_to_cpa_s=240.0,
        radius_entry_s=210.0,
        closest_distance_km=2.1,
        path=[
            _DummyPathPoint(30.0, 41.3100, 28.7410, 14.0),
            _DummyPathPoint(60.0, 41.2920, 28.7410, 12.0),  # lands at 17L threshold at t=60s (< 210s)
        ],
    )
    airport_arr = evaluate_airport_arrival_v6(
        aircraft=ac_arr,
        prediction=pred_arr,
        samples=arr_samples,
        user_lat=41.1800,
        user_lon=28.7410,
        alert_radius_km=6.0,
        candidate_airport=airport,
        destination_airport=_DummyDestination(),
        route_plausible=True,
    )
    assert airport_arr.classification == "ARRIVAL"
    assert airport_arr.suppress_alert is True
    assert airport_arr.matched_runway_id == "17L"

    rec_supp = collector.capture(
        icao=ac_arr.icao24,
        callsign=ac_arr.callsign,
        observer_lat=41.1800,
        observer_lon=28.7410,
        observation_age_s=1.2,
        predicted_cpa_km=2.1,
        predicted_eta_s=240.0,
        airport_eval=airport_arr,
        final_decision="suppress",
        stable_reason_code=airport_arr.reason_code,
    )
    assert rec_supp is not None
    assert rec_supp.final_decision == "suppress"
    assert rec_supp.airport_classification == "ARRIVAL"
    assert rec_supp.runway_id == "17L"

    # Structured 5-way user feedback
    for fb in USER_FEEDBACK_CATEGORIES_V6:
        assert normalize_v6_feedback_reason(fb) == fb
        assert fb in V6_FEEDBACK_LABELS
    updated = collector.record_feedback(rec_alert.record_id, "saw_it", note="Clear overhead pass")
    assert updated.user_feedback == "saw_it"

    # Missing or stale coverage must be recorded as unknown, never hit or miss
    fresh_state, cov_state = classify_observation_freshness(observation_age_s=95.0, has_position=True)
    assert fresh_state == "stale"
    assert cov_state == "unknown"
    rec_stale = collector.capture(
        icao="4BB999",
        observation_age_s=95.0,
        final_decision="alert",
        stable_reason_code="STALE_OBSERVATION_CHECK",
    )
    assert rec_stale is not None
    assert rec_stale.coverage_status == "unknown"
    assert rec_stale.final_decision == "unknown_coverage"

    # Disabled collector performs no capture
    collector.enabled = False
    assert collector.capture(icao="4BB000") is None


def test_phase2_triage_into_five_distinct_problem_classes() -> None:
    c1 = triage_fresh_case(
        case_id="case_freshness_1",
        main_sha=DEFAULT_PINNED_MAIN_SHA,
        observed_inputs={"observation_age_s": 74.0, "conflicting_sources": True},
        commit_behavior="Extrapolated stale ADS-B sample",
        expected_result="Mark coverage unknown and suppress unverified extrapolation",
    )
    assert c1.problem_class == "DATA_FRESHNESS"

    c2 = triage_fresh_case(
        case_id="case_eta_2",
        main_sha=DEFAULT_PINNED_MAIN_SHA,
        observed_inputs={"eta_drift": True, "turn_detected": True},
        commit_behavior="Linear projection lagged 32deg base turn",
        expected_result="Update ETA/CPA with turn-aware kinematic damping",
    )
    assert c2.problem_class == "ETA_TRAJECTORY"

    c3 = triage_fresh_case(
        case_id="case_airport_3",
        main_sha=DEFAULT_PINNED_MAIN_SHA,
        observed_inputs={"airport_misclassified": True},
        commit_behavior="Treated runway 17L final approach as observer pass",
        expected_result="Suppress with AIRPORT_SUPPRESS_VERIFIED_ARRIVAL",
    )
    assert c3.problem_class == "AIRPORT_LOGIC"

    c4 = triage_fresh_case(
        case_id="case_terrain_4",
        main_sha=DEFAULT_PINNED_MAIN_SHA,
        observed_inputs={"terrain_blocked_pass": True},
        commit_behavior="Alerted low pass behind 420m ridge",
        expected_result="Return terrain LOS blocked with ridge clearance deficit",
    )
    assert c4.problem_class == "TERRAIN_VISIBILITY"

    c5 = triage_fresh_case(
        case_id="case_lifecycle_5",
        main_sha=DEFAULT_PINNED_MAIN_SHA,
        observed_inputs={"duplicate_notification": True},
        commit_behavior="Sent duplicate alert after provider handoff",
        expected_result="Deduplicate within active pass lifecycle",
    )
    assert c5.problem_class == "NOTIFICATION_LIFECYCLE"


def test_phase3a_airport_arrival_detection_and_genuine_pass_protection() -> None:
    airport = _sample_ltfm_geometry()

    # 1. Runway threshold match on final approach (aligned with 17L, heading 180)
    rw_match = match_runway_threshold(
        aircraft_lat=41.3300,
        aircraft_lon=28.7410,
        aircraft_agl_m=420.0,
        aircraft_heading_deg=180.0,
        airport=airport,
    )
    assert rw_match is not None
    assert rw_match.runway_id == "17L"
    assert rw_match.aligned_final is True

    # 2. Genuine observer pass BEFORE the runway must NOT be suppressed
    ac_pass_first = _DummyAircraft(
        latitude=41.3600,
        longitude=28.7410,
        altitude=720.0,
        ground_speed=155.0,
        heading=180.0,
        vertical_rate_mps=-3.2,
    )
    pred_pass_first = _DummyPrediction(
        time_to_cpa_s=22.0,
        radius_entry_s=10.0,
        closest_distance_km=0.4,
        path=[
            _DummyPathPoint(22.0, 41.3400, 28.7410, 0.4),
            _DummyPathPoint(95.0, 41.2930, 28.7410, 5.2),
        ],
    )
    # Observer is at 41.3400 (north of 17L threshold at 41.2930), so aircraft passes observer BEFORE landing
    eval_pass_first = evaluate_airport_arrival_v6(
        aircraft=ac_pass_first,
        prediction=pred_pass_first,
        samples=[
            TerminalSample(1000.0, 41.3700, 28.7410, 780.0, 155.0, 180.0, -3.2, 1.0),
            TerminalSample(1010.0, 41.3600, 28.7410, 720.0, 155.0, 180.0, -3.2, 1.0),
        ],
        user_lat=41.3400,
        user_lon=28.7410,
        alert_radius_km=6.0,
        candidate_airport=airport,
        destination_airport=_DummyDestination(),
        route_plausible=True,
    )
    assert eval_pass_first.observer_before_airport is True
    assert eval_pass_first.suppress_alert is False

    # 3. Missed approach / go-around releases suppression immediately
    ac_go_around = _DummyAircraft(
        latitude=41.2600,
        longitude=28.7410,
        altitude=650.0,
        ground_speed=175.0,
        heading=180.0,
        vertical_rate_mps=7.5,  # climbing sharply away after low approach
    )
    track_go_around = [
        TerminalSample(100.0, 41.2750, 28.7410, 380.0, 145.0, 180.0, -3.5, 1.0),
        TerminalSample(110.0, 41.2680, 28.7410, 480.0, 160.0, 180.0, 5.0, 1.0),
        TerminalSample(120.0, 41.2600, 28.7410, 650.0, 175.0, 180.0, 7.5, 1.0),
    ]
    eval_go_around = evaluate_airport_arrival_v6(
        aircraft=ac_go_around,
        prediction=_DummyPrediction(time_to_cpa_s=65.0, radius_entry_s=40.0),
        samples=track_go_around,
        user_lat=41.2100,
        user_lon=28.7410,
        alert_radius_km=8.0,
        candidate_airport=airport,
        destination_airport=_DummyDestination(),
        route_plausible=True,
    )
    assert eval_go_around.classification == "MISSED_APPROACH"
    assert eval_go_around.suppress_alert is False

    # 4. High-altitude overflight is never misclassified as arrival
    ac_overflight = _DummyAircraft(
        latitude=41.2800,
        longitude=28.7500,
        altitude=10500.0,
        ground_speed=460.0,
        heading=115.0,
        vertical_rate_mps=0.0,
    )
    eval_overflight = evaluate_airport_arrival_v6(
        aircraft=ac_overflight,
        prediction=_DummyPrediction(time_to_cpa_s=45.0, radius_entry_s=20.0),
        samples=[
            TerminalSample(100.0, 41.2900, 28.7200, 10500.0, 460.0, 115.0, 0.0, 1.0),
            TerminalSample(110.0, 41.2800, 28.7500, 10500.0, 460.0, 115.0, 0.0, 1.0),
        ],
        user_lat=41.2500,
        user_lon=28.7500,
        alert_radius_km=8.0,
        candidate_airport=airport,
    )
    assert eval_overflight.classification == "OVERFLIGHT"
    assert eval_overflight.suppress_alert is False


def test_phase3b_terrain_line_of_sight_with_curvature_and_unknown_fallbacks() -> None:
    # Verify 4/3 Earth curvature drop (~5.89m at 10 km)
    drop_10km = earth_curvature_drop_m(10.0)
    assert 5.5 < drop_10km < 6.3

    # 1. Obstructed by verified ridge along sightline -> blocked
    blocked_res = evaluate_terrain_line_of_sight(
        observer_lat=41.0000,
        observer_lon=29.0000,
        observer_elevation_m=50.0,
        aircraft_lat=41.0900,
        aircraft_lon=29.0000,
        aircraft_altitude_m=420.0,
        profile=TerrainProfileData(
            points=(
                TerrainProfilePoint(distance_km=2.0, elevation_m=90.0),
                TerrainProfilePoint(distance_km=4.5, elevation_m=380.0),
                TerrainProfilePoint(distance_km=6.8, elevation_m=150.0),
                TerrainProfilePoint(distance_km=8.5, elevation_m=110.0),
            ),
            data_version="SRTM_30M_V6",
            quality="verified",
            resolution_m=30.0,
            source="srtm30m",
        ),
    )
    assert blocked_res.status == "blocked"
    assert blocked_res.blocking_terrain_elevation_m == 380.0
    assert blocked_res.visual_conditions_included is False

    # 2. Coarse DEM resolution (> 180m) must return unknown, never blocked
    coarse_res = evaluate_terrain_line_of_sight(
        observer_lat=41.0000,
        observer_lon=29.0000,
        observer_elevation_m=50.0,
        aircraft_lat=41.0900,
        aircraft_lon=29.0000,
        aircraft_altitude_m=420.0,
        profile=TerrainProfileData(
            points=(
                TerrainProfilePoint(distance_km=2.5, elevation_m=380.0),
                TerrainProfilePoint(distance_km=5.0, elevation_m=390.0),
            ),
            data_version="ETOPO_COARSE_1KM",
            quality="verified",
            resolution_m=1000.0,
        ),
    )
    assert coarse_res.status == "unknown"
    assert coarse_res.terrain_quality == "coarse"

    # 3. Missing observer elevation must return unknown
    missing_obs = evaluate_terrain_line_of_sight(
        observer_lat=41.0000,
        observer_lon=29.0000,
        observer_elevation_m=None,
        aircraft_lat=41.0900,
        aircraft_lon=29.0000,
        aircraft_altitude_m=1200.0,
    )
    assert missing_obs.status == "unknown"
    assert missing_obs.reason_code == "TERRAIN_UNKNOWN_OBSERVER_ELEVATION"


def test_phase3c_eta_cpa_diagnosis_and_confirmed_pass_scoring() -> None:
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"

    # Sub-step linear interpolation for radius entry
    path_pts = [
        _DummyPathPoint(0.0, 41.0, 29.0, 10.0),
        _DummyPathPoint(20.0, 41.01, 29.0, 9.0),
        _DummyPathPoint(40.0, 41.02, 29.0, 7.0),  # crosses 8.0 km halfway between t=20 and t=40 -> t=30s
    ]
    refined_entry = refine_age_aware_radius_entry_s(path_pts, alert_radius_km=8.0)
    assert refined_entry == 30.0

    # Root-cause attribution for old observations vs changing trajectory
    old_samples = [
        HistorySample(1000.0, 41.05, 28.98, 1000.0, 220.0, 90.0, 0.0, 22.0),
    ]
    diag_old = diagnose_eta_cpa(
        prediction=_DummyPrediction(time_to_cpa_s=50.0, stale=True),
        samples=old_samples,
        alert_radius_km=8.0,
        now=1025.0,
    )
    assert diag_old.primary_driver == "OLD_OBSERVATIONS"

    turn_samples = [
        HistorySample(1000.0, 41.05, 28.96, 1000.0, 220.0, 60.0, 0.0, 1.0),
        HistorySample(1005.0, 41.05, 28.97, 1000.0, 220.0, 75.0, 0.0, 1.0),
        HistorySample(1010.0, 41.05, 28.98, 1000.0, 220.0, 95.0, 0.0, 1.0),
    ]
    diag_turn = diagnose_eta_cpa(
        prediction=_DummyPrediction(time_to_cpa_s=50.0, turn_rate_deg_s=2.4, heading_stability_deg=25.0),
        samples=turn_samples,
        alert_radius_km=8.0,
        now=1011.0,
    )
    assert diag_turn.primary_driver == "CHANGING_TRAJECTORY"

    # ETA outcome evaluation: confirmed pass vs uncovered/missing coverage
    scored = evaluate_confirmed_pass_eta_error(
        predicted_at_epoch_s=1000.0,
        predicted_time_to_cpa_s=60.0,
        predicted_cpa_km=2.0,
        observed_pass=True,
        coverage_missing_or_stale=False,
        observed_closest_epoch_s=1064.0,
        observed_closest_km=2.4,
        alert_radius_km=8.0,
        diagnosis=diag_turn,
    )
    assert scored.status == "scored_observed_pass"
    assert scored.scoreable is True
    assert scored.absolute_eta_error_s == 4.0

    inconclusive = evaluate_confirmed_pass_eta_error(
        predicted_at_epoch_s=1000.0,
        predicted_time_to_cpa_s=60.0,
        predicted_cpa_km=2.0,
        observed_pass=False,
        coverage_missing_or_stale=True,
        alert_radius_km=8.0,
    )
    assert inconclusive.status == "inconclusive_missing_coverage"
    assert inconclusive.scoreable is False
    assert inconclusive.absolute_eta_error_s is None


def test_phase4_release_validation_metrics_and_denominators() -> None:
    eta_evals = [
        evaluate_confirmed_pass_eta_error(
            predicted_at_epoch_s=1000.0,
            predicted_time_to_cpa_s=60.0,
            predicted_cpa_km=1.8,
            observed_pass=True,
            coverage_missing_or_stale=False,
            observed_closest_epoch_s=1063.0,
            observed_closest_km=1.8,
            alert_radius_km=8.0,
        ),
        evaluate_confirmed_pass_eta_error(
            predicted_at_epoch_s=2000.0,
            predicted_time_to_cpa_s=90.0,
            predicted_cpa_km=2.4,
            observed_pass=True,
            coverage_missing_or_stale=False,
            observed_closest_epoch_s=2095.0,
            observed_closest_km=2.5,
            alert_radius_km=8.0,
        ),
        evaluate_confirmed_pass_eta_error(
            predicted_at_epoch_s=3000.0,
            predicted_time_to_cpa_s=75.0,
            predicted_cpa_km=2.1,
            observed_pass=False,
            coverage_missing_or_stale=True,
            alert_radius_km=8.0,
        ),
    ]
    metrics = compute_v6_success_measures(
        observer_hours=24.0,
        alert_outcomes=[
            {"coverage_status": "confirmed", "is_false_alert": False},
            {"coverage_status": "confirmed", "is_false_alert": False},
            {"coverage_status": "unknown", "is_false_alert": True},
        ],
        visible_pass_outcomes=[
            {"coverage_status": "confirmed", "terrain_los_status": "clear", "confirmed_in_radius_pass": True, "alert_delivered": True},
            {"coverage_status": "unknown", "terrain_los_status": "unknown", "confirmed_in_radius_pass": True, "alert_delivered": False},
        ],
        eta_evaluations=eta_evals,
        airport_evaluations=[
            {"expected_class": "ARRIVAL", "predicted_class": "ARRIVAL", "coverage_status": "confirmed"},
            {"expected_class": "DEPARTURE", "predicted_class": "DEPARTURE", "coverage_status": "confirmed"},
            {"expected_class": "OVERFLIGHT", "predicted_class": "OVERFLIGHT", "coverage_status": "confirmed"},
            {"expected_class": "HOLDING", "predicted_class": "HOLDING", "coverage_status": "confirmed"},
            {"expected_class": "MISSED_APPROACH", "predicted_class": "MISSED_APPROACH", "coverage_status": "confirmed"},
        ],
        terrain_evaluations=[
            {"predicted_los": "blocked", "actual_obstructed": True},
            {"predicted_los": "clear", "actual_obstructed": False},
            {"predicted_los": "unknown", "actual_obstructed": None},
        ],
        lifecycle_events=[
            {"duplicate_send": False, "wrong_cancellation": False, "delivery_failed": False},
            {"duplicate_send": False, "wrong_cancellation": False, "delivery_failed": False},
        ],
    )
    assert metrics.confirmed_false_alerts_count == 0
    assert metrics.observer_hours_denominator == 24.0
    assert metrics.false_alert_unknown_coverage_count == 1
    assert metrics.confirmed_missed_visible_passes_count == 0
    assert metrics.confirmed_visible_passes_denominator == 1
    assert metrics.visibility_unknown_coverage_count == 1
    assert metrics.eta_confirmed_passes_denominator == 2
    assert metrics.eta_unavailable_or_uncertain_count == 1
    assert metrics.eta_mae_s == 4.0
    assert metrics.terrain_false_clear_count == 0
    assert metrics.terrain_unknown_count == 1
    assert metrics.delivery_cancellation_error_count == 0
    assert metrics.monitoring_loop_healthy is True

def test_phase0_pinned_main_sha_matches_all_release_artifacts() -> None:
    from app.version import PINNED_MAIN_SHA

    assert DEFAULT_PINNED_MAIN_SHA == PINNED_MAIN_SHA
    assert re.fullmatch(r"[0-9a-f]{40}", PINNED_MAIN_SHA)
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "src/lib/planeAlertsV6Engine.ts",
        "docs/releases/v6.0.0.md",
        "CHANGELOG.md",
    ):
        assert PINNED_MAIN_SHA in (root / relative).read_text(encoding="utf-8")
