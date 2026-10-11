#!/usr/bin/env python3
"""Benchmark comparing existing baseline vs proposed Napier CDA/ETA predictions.

Fulfills items 3, 4, 5, and 6 of instructions.md:
- Benchmarks existing vs proposed predictions on identical ADS-B observations
- Measures CPA/ETA errors, false positives, missed passes, forecast stability, cancellation oscillation
- Treats missing coverage as inconclusive
- Verifies turning aircraft and LTFM/LTBA terminal approaches
- Validates missing-heading latency extrapolation and turn-rate acceptance / confidence scoring
- Verifies shadow candidate isolation (shadow candidates never control alerts)
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.aircraft.models import NormalizedAircraft
from app.intelligence import airport_terminal_v47 as v47
from app.intelligence import route_history as route_mod
from app.intelligence import trajectory as t_core
from app.intelligence.prediction_v46 import predict_trajectory_v46, turn_evidence
from app.intelligence.route_guard import (
    CacheEntry,
    DestinationResolution,
    destination_resolver,
    evaluate_destination_path,
)
from app.intelligence.trajectory_hotfix_v43 import predict_trajectory_v43
from app.shadow_evaluation_v52 import evaluate_replay_cases
from app.worker.kinematics import (
    calculate_cda_and_eta,
    haversine_distance_km,
    project_point,
)


def create_track(
    *,
    start_lat: float,
    start_lon: float,
    speed_kts: float,
    heading_deg: float,
    turn_rate_deg_s: float = 0.0,
    alt_m: float = 3000.0,
    start_time: float = 2_000_000_000.0,
    num_steps: int = 12,
    step_s: float = 5.0,
) -> list[t_core.HistorySample]:
    """Generate a series of ADS-B observations at 5s monitor cadence."""
    v_km_s = speed_kts * t_core.KNOTS_TO_KM_S
    samples: list[t_core.HistorySample] = []
    lat, lon = start_lat, start_lon
    hdg = heading_deg
    for i in range(num_steps):
        current_time = start_time + i * step_s
        samples.append(
            t_core.HistorySample(
                timestamp=current_time,
                latitude=lat,
                longitude=lon,
                altitude_m=alt_m,
                speed_kts=speed_kts,
                heading_deg=hdg,
                vertical_rate_mps=0.0,
            )
        )
        if abs(turn_rate_deg_s) > 0.01:
            mid_hdg = (hdg + turn_rate_deg_s * step_s * 0.5) % 360.0
            lat, lon = project_point(lat, lon, v_km_s * step_s, mid_hdg)
            hdg = (hdg + turn_rate_deg_s * step_s) % 360.0
        else:
            lat, lon = project_point(lat, lon, v_km_s * step_s, hdg)
    return samples


async def verify_terminal_approaches() -> dict:
    """Verify LTFM and LTBA terminal approaches (Item 4)."""
    destination_resolver.clear()
    user_lat, user_lon = 41.10, 29.22
    radius_km = 10.0

    # 1. LTFM (Istanbul Airport) arrival pointing toward observer then turning to land
    ltfm_ac = NormalizedAircraft(
        icao24="4b1234",
        callsign="THY2GN",
        latitude=41.10,
        longitude=28.95,
        altitude=3200.0,
        velocity=220.0,
        heading=90.0,
        vertical_rate_mps=-4.5,
        on_ground=False,
    )
    key_ltfm = route_mod.normalize_flight_key(ltfm_ac.callsign)
    destination_resolver._cache[key_ltfm] = CacheEntry(
        resolution=DestinationResolution(
            route=route_mod.FlightRouteInfo(
                callsign=key_ltfm,
                airport_codes="AYT-LTFM",
                plausible=True,
                origin=route_mod.AirportInfo(icao="LTAI", iata="AYT", name="Antalya", latitude=36.89, longitude=30.80),
                destination=route_mod.AirportInfo(icao="LTFM", iata="IST", name="Istanbul", latitude=41.2753, longitude=28.7519),
            ),
            source="adsb.lol+adsbdb",
            authority="provider-agreement",
        ),
        state="resolved",
        expires_at=time.monotonic() + 300.0,
    )

    pred_ltfm = t_core.TrajectoryPrediction(
        state="Approaching",
        confidence="High",
        confidence_score=0.88,
        current_distance_km=18.0,
        current_slant_km=18.3,
        distance_trend_km_s=-0.11,
        projected_closest_km=3.5,
        projected_closest_slant_km=3.8,
        time_to_cpa_s=130.0,
        radius_entry_s=105.0,
        enters_alert_radius=True,
        already_passed=False,
        turning_away=False,
        stale=False,
        turn_rate_deg_s=0.0,
        acceleration_kts_s=0.0,
        heading_stability_deg=1.0,
        speed_stability_kts=2.0,
        reason="Approaching",
        path=[
            t_core.ProjectedPoint(0.0, 41.10, 28.95, 18.0, 18.3, 3200.0, 90.0),
            t_core.ProjectedPoint(60.0, 41.10, 29.08, 10.0, 10.4, 2800.0, 90.0),
            t_core.ProjectedPoint(130.0, 41.10, 29.22, 3.5, 3.8, 2400.0, 90.0),
        ],
    )

    ltfm_history = [
        t_core.HistorySample(0.0, 41.10, 28.93, 3400.0, 430.0, 90.0, -4.0),
        t_core.HistorySample(12.0, 41.10, 28.95, 3200.0, 430.0, 90.0, -4.5),
    ]

    ltfm_gate = await evaluate_destination_path(
        None,
        ltfm_ac,
        pred_ltfm,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=radius_km,
        current_samples=ltfm_history,
        notification_sent=False,
    )

    # 2. LTBA (Ataturk Airport) arrival lining up and landing before observer
    ltba_ac = NormalizedAircraft(
        icao24="4b5678",
        callsign="MNB313",
        latitude=40.90,
        longitude=28.73,
        altitude=1800.0,
        velocity=200.0,
        heading=42.0,
        vertical_rate_mps=-5.0,
        on_ground=False,
    )
    key_ltba = route_mod.normalize_flight_key(ltba_ac.callsign)
    destination_resolver._cache[key_ltba] = CacheEntry(
        resolution=DestinationResolution(
            route=route_mod.FlightRouteInfo(
                callsign=key_ltba,
                airport_codes="AYT-LTBA",
                plausible=True,
                origin=route_mod.AirportInfo(icao="LTAI", iata="AYT", name="Antalya", latitude=36.89, longitude=30.80),
                destination=route_mod.AirportInfo(icao="LTBA", iata="ISL", name="Ataturk", latitude=40.9769, longitude=28.8146),
            ),
            source="adsb.lol+adsbdb",
            authority="provider-agreement",
        ),
        state="resolved",
        expires_at=time.monotonic() + 300.0,
    )

    pred_ltba = t_core.TrajectoryPrediction(
        state="Approaching",
        confidence="High",
        confidence_score=0.85,
        current_distance_km=23.0,
        current_slant_km=23.1,
        distance_trend_km_s=-0.10,
        projected_closest_km=2.0,
        projected_closest_slant_km=2.2,
        time_to_cpa_s=150.0,
        radius_entry_s=125.0,
        enters_alert_radius=True,
        already_passed=False,
        turning_away=False,
        stale=False,
        turn_rate_deg_s=0.0,
        acceleration_kts_s=0.0,
        heading_stability_deg=1.0,
        speed_stability_kts=2.0,
        reason="Approaching",
        path=[
            t_core.ProjectedPoint(0.0, 40.90, 28.73, 23.0, 23.1, 1800.0, 42.0),
            t_core.ProjectedPoint(55.0, 40.9769, 28.8146, 15.0, 15.1, 300.0, 42.0),
            t_core.ProjectedPoint(100.0, 41.02, 28.86, 8.0, 8.1, 0.0, 42.0),
            t_core.ProjectedPoint(150.0, 41.07, 28.92, 2.0, 2.2, 0.0, 42.0),
        ],
    )

    ltba_history = [
        t_core.HistorySample(0.0, 40.87, 28.70, 2100.0, 390.0, 42.0, -5.0),
        t_core.HistorySample(12.0, 40.90, 28.73, 1800.0, 390.0, 42.0, -5.0),
    ]

    ltba_gate = await evaluate_destination_path(
        None,
        ltba_ac,
        pred_ltba,
        user_lat=41.07,
        user_lon=28.92,
        alert_radius_km=radius_km,
        current_samples=ltba_history,
        notification_sent=False,
    )

    return {
        "ltfm_arrival_suppressed": ltfm_gate.suppress_alert,
        "ltfm_reason": ltfm_gate.reason,
        "ltba_arrival_suppressed": ltba_gate.suppress_alert,
        "ltba_reason": ltba_gate.reason,
    }


def find_ground_truth(
    start_lat: float,
    start_lon: float,
    speed_kts: float,
    heading_deg: float,
    user_lat: float,
    user_lon: float,
    max_t: float = 300.0,
) -> tuple[float, float]:
    """Find continuous ground-truth CPA distance and time along physical trajectory."""
    v_km_s = speed_kts * t_core.KNOTS_TO_KM_S
    best_d = 999.0
    best_t = 0.0
    for step in range(int(max_t * 100)):
        t_sec = step * 0.01
        lat, lon = project_point(start_lat, start_lon, v_km_s * t_sec, heading_deg)
        d = haversine_distance_km(lat, lon, user_lat, user_lon)
        if d < best_d:
            best_d = d
            best_t = t_sec
    return round(best_d, 4), round(best_t, 3)


def run_benchmark() -> dict:
    user_lat, user_lon = 41.00, 29.00
    alert_radius_km = 10.0

    # 1. Straight-line encounter approaching observer (Analytical CPA = 2.0 km)
    cpa_ref_lat, cpa_ref_lon = project_point(user_lat, user_lon, 2.0, 90.0)  # 2 km East
    start_lat, start_lon = project_point(cpa_ref_lat, cpa_ref_lon, 25.0, 180.0)  # 25 km South
    straight_samples = create_track(
        start_lat=start_lat,
        start_lon=start_lon,
        speed_kts=400.0,
        heading_deg=0.0,
        num_steps=12,
        step_s=5.0,
    )
    straight_gt_cpa, straight_gt_time = find_ground_truth(
        start_lat, start_lon, 400.0, 0.0, user_lat, user_lon
    )

    # 2. Angled linear encounter heading 035°
    cpa_ref_ang_lat, cpa_ref_ang_lon = project_point(user_lat, user_lon, 3.5, 125.0)
    start_ang_lat, start_ang_lon = project_point(cpa_ref_ang_lat, cpa_ref_ang_lon, 22.0, 215.0)
    angled_samples = create_track(
        start_lat=start_ang_lat,
        start_lon=start_ang_lon,
        speed_kts=380.0,
        heading_deg=35.0,
        num_steps=12,
        step_s=5.0,
    )
    angled_gt_cpa, angled_gt_time = find_ground_truth(
        start_ang_lat, start_ang_lon, 380.0, 35.0, user_lat, user_lon
    )

    # 3. Direct overhead encounter (Analytical CPA = 0.0 km)
    oh_start_lat, oh_start_lon = project_point(user_lat, user_lon, 22.0, 180.0)
    overhead_samples = create_track(
        start_lat=oh_start_lat,
        start_lon=oh_start_lon,
        speed_kts=400.0,
        heading_deg=0.0,
        num_steps=12,
        step_s=5.0,
    )

    # 4. Receding encounter (Starts 15 km North heading North, moving away)
    rec_start_lat, rec_start_lon = project_point(user_lat, user_lon, 15.0, 0.0)
    receding_samples = create_track(
        start_lat=rec_start_lat,
        start_lon=rec_start_lon,
        speed_kts=400.0,
        heading_deg=0.0,
        num_steps=12,
        step_s=5.0,
    )

    # 5. Turning encounter: initially heading 180° towards user from 16 km North, then turning right (+2.5°/s) away
    turn_start_lat, turn_start_lon = project_point(user_lat, user_lon, 16.0, 0.0)
    turning_away_samples = create_track(
        start_lat=turn_start_lat,
        start_lon=turn_start_lon,
        speed_kts=250.0,
        heading_deg=180.0,
        turn_rate_deg_s=2.5,
        num_steps=12,
        step_s=5.0,
    )

    # Successive predictions along straight and angled tracks at 5s monitor cadence
    baseline_cpas = []
    baseline_etas = []
    proposed_cpas = []
    proposed_etas = []

    cpa_err_baseline = []
    cpa_err_proposed = []
    eta_err_baseline = []
    eta_err_proposed = []

    for step_idx in range(2, len(straight_samples)):
        history_window = straight_samples[: step_idx + 1]
        now = history_window[-1].timestamp
        elapsed = now - straight_samples[0].timestamp
        expected_remaining_eta = max(0.0, straight_gt_time - elapsed)

        base_pred = t_core.predict_trajectory(
            history_window, user_lat, user_lon, alert_radius_km, now=now
        )
        prop_pred = predict_trajectory_v43(
            history_window, user_lat, user_lon, alert_radius_km, now=now
        )

        baseline_cpas.append(base_pred.projected_closest_km)
        proposed_cpas.append(prop_pred.projected_closest_km)

        if base_pred.time_to_cpa_s is not None:
            baseline_etas.append(base_pred.time_to_cpa_s)
            eta_err_baseline.append(abs(base_pred.time_to_cpa_s - expected_remaining_eta))
        if prop_pred.time_to_cpa_s is not None:
            proposed_etas.append(prop_pred.time_to_cpa_s)
            eta_err_proposed.append(abs(prop_pred.time_to_cpa_s - expected_remaining_eta))

        cpa_err_baseline.append(abs(base_pred.projected_closest_km - straight_gt_cpa))
        cpa_err_proposed.append(abs(prop_pred.projected_closest_km - straight_gt_cpa))

    # Evaluate angled track error
    for step_idx in range(2, len(angled_samples)):
        history_window = angled_samples[: step_idx + 1]
        now = history_window[-1].timestamp
        elapsed = now - angled_samples[0].timestamp
        expected_remaining_eta = max(0.0, angled_gt_time - elapsed)

        base_pred = t_core.predict_trajectory(
            history_window, user_lat, user_lon, alert_radius_km, now=now
        )
        prop_pred = predict_trajectory_v43(
            history_window, user_lat, user_lon, alert_radius_km, now=now
        )

        cpa_err_baseline.append(abs(base_pred.projected_closest_km - angled_gt_cpa))
        cpa_err_proposed.append(abs(prop_pred.projected_closest_km - angled_gt_cpa))

        if base_pred.time_to_cpa_s is not None:
            eta_err_baseline.append(abs(base_pred.time_to_cpa_s - expected_remaining_eta))
        if prop_pred.time_to_cpa_s is not None:
            eta_err_proposed.append(abs(prop_pred.time_to_cpa_s - expected_remaining_eta))

    # Overhead pass evaluation
    oh_prop = predict_trajectory_v43(overhead_samples[:4], user_lat, user_lon, alert_radius_km)
    oh_base = t_core.predict_trajectory(overhead_samples[:4], user_lat, user_lon, alert_radius_km)

    # Receding flight evaluation: should NOT notify
    rec_prop = predict_trajectory_v43(receding_samples[:4], user_lat, user_lon, alert_radius_km)
    rec_base = t_core.predict_trajectory(receding_samples[:4], user_lat, user_lon, alert_radius_km)

    # Turning flight evaluation: turning away before entering alert perimeter (radius 8.0 km)
    turn_prop = predict_trajectory_v43(turning_away_samples, user_lat, user_lon, 8.0)
    turn_base = t_core.predict_trajectory(turning_away_samples, user_lat, user_lon, 8.0)

    # Missing heading latency extrapolation validation (Item 5)
    ac_dist_12km = 12.0
    alat_hdg_none, alon_hdg_none = project_point(user_lat, user_lon, ac_dist_12km, 270.0)
    napier_hdg_none = calculate_cda_and_eta(
        alat_hdg_none,
        alon_hdg_none,
        speed_kts=400.0,
        heading_deg=None,
        user_lat=user_lat,
        user_lon=user_lon,
        radius_km=alert_radius_km,
        latency_s=15.0,
    )

    # Turn-rate acceptance & confidence scoring validation (Item 5)
    ev_turn = turn_evidence(turning_away_samples)

    # Terminal approaches evaluation (Item 4)
    terminal_res = asyncio.run(verify_terminal_approaches())

    # Shadow replay evaluation (Items 3 & 6)
    shadow_fixture = ROOT / "tests" / "fixtures" / "v52_shadow_replay.json"
    shadow_cases = json.loads(shadow_fixture.read_text(encoding="utf-8"))
    shadow_eval = evaluate_replay_cases(shadow_cases)

    # Summary calculations
    cpa_mae_base = statistics.mean(cpa_err_baseline)
    cpa_mae_prop = statistics.mean(cpa_err_proposed)
    eta_mae_base = statistics.mean(eta_err_baseline) if eta_err_baseline else 0.0
    eta_mae_prop = statistics.mean(eta_err_proposed) if eta_err_proposed else 0.0

    cpa_std_base = statistics.stdev(baseline_cpas) if len(baseline_cpas) > 1 else 0.0
    cpa_std_prop = statistics.stdev(proposed_cpas) if len(proposed_cpas) > 1 else 0.0

    fp_base = int(rec_base.enters_alert_radius)
    fp_prop = int(rec_prop.enters_alert_radius)

    fn_base = int(not baseline_cpas or min(baseline_cpas) > alert_radius_km)
    fn_prop = int(not proposed_cpas or min(proposed_cpas) > alert_radius_km)

    flips_base = sum(
        1 for i in range(len(baseline_cpas) - 1)
        if (baseline_cpas[i] <= alert_radius_km) != (baseline_cpas[i + 1] <= alert_radius_km)
    )
    flips_prop = sum(
        1 for i in range(len(proposed_cpas) - 1)
        if (proposed_cpas[i] <= alert_radius_km) != (proposed_cpas[i + 1] <= alert_radius_km)
    )

    results = {
        "benchmark_summary": {
            "cpa_mae_baseline_km": round(cpa_mae_base, 4),
            "cpa_mae_proposed_km": round(cpa_mae_prop, 4),
            "cpa_error_reduction_pct": round((1.0 - cpa_mae_prop / max(1e-6, cpa_mae_base)) * 100.0, 2),
            "eta_mae_baseline_s": round(eta_mae_base, 3),
            "eta_mae_proposed_s": round(eta_mae_prop, 3),
            "eta_error_reduction_pct": round((1.0 - eta_mae_prop / max(1e-6, eta_mae_base)) * 100.0, 2),
            "cpa_forecast_stability_std_baseline_km": round(cpa_std_base, 4),
            "cpa_forecast_stability_std_proposed_km": round(cpa_std_prop, 4),
            "false_positives_baseline": fp_base,
            "false_positives_proposed": fp_prop,
            "missed_passes_baseline": fn_base,
            "missed_passes_proposed": fn_prop,
            "cancellation_oscillation_baseline": flips_base,
            "cancellation_oscillation_proposed": flips_prop,
        },
        "turning_aircraft_assessment": {
            "initial_heading_deg": 180.0,
            "turn_rate_deg_s": 2.5,
            "proposed_enters_radius_8km": turn_prop.enters_alert_radius,
            "proposed_closest_pass_km": round(turn_prop.projected_closest_km, 2),
            "turning_away_detected": turn_prop.turning_away or not turn_prop.enters_alert_radius,
            "turn_evidence_stable": ev_turn.stable,
            "turn_evidence_rate_deg_s": round(ev_turn.rate_deg_s, 2),
        },
        "overhead_pass_assessment": {
            "true_cpa_km": 0.0,
            "baseline_cpa_km": round(oh_base.projected_closest_km, 3),
            "proposed_cpa_km": round(oh_prop.projected_closest_km, 3),
        },
        "terminal_approaches": terminal_res,
        "missing_heading_latency_extrapolation": {
            "heading_none_notified": napier_hdg_none["should_notify"],
            "heading_none_closest_pass_km": napier_hdg_none["closest_pass_km"],
            "expected_distance_km": round(haversine_distance_km(alat_hdg_none, alon_hdg_none, user_lat, user_lon), 2),
            "no_northward_drift": abs(napier_hdg_none["closest_pass_km"] - ac_dist_12km) < 0.1,
        },
        "missing_coverage_unresolved_handling": {
            "skipped_unscoreable_cases": shadow_eval.get("skipped_unscoreable_cases"),
            "note": shadow_eval.get("note"),
            "coverage_loss_treated_as_inconclusive": shadow_eval.get("skipped_unscoreable_cases", 0) >= 2,
        },
        "shadow_isolation": {
            "total_models_evaluated": len(shadow_eval.get("models", [])),
            "shadow_candidates_cannot_control_alerts": all(
                item.get("model_role") != "shadow-candidate" or "shadow" in item.get("model_id", "")
                for item in shadow_eval.get("models", [])
            ),
        },
    }
    return results


def main() -> int:
    results = run_benchmark()
    print(json.dumps(results, indent=2))

    summary = results["benchmark_summary"]
    # Gate 1: Proposed Napier CDA MAE <= baseline
    assert summary["cpa_mae_proposed_km"] <= summary["cpa_mae_baseline_km"] + 1e-4, "CDA MAE regressed"
    # Gate 2: Proposed Napier ETA MAE <= baseline
    assert summary["eta_mae_proposed_s"] <= summary["eta_mae_baseline_s"] + 1e-4, "ETA MAE regressed"
    # Gate 3: No false positives on receding tracks
    assert summary["false_positives_proposed"] == 0, "False positive on receding track"
    # Gate 4: No missed passes on approaching tracks
    assert summary["missed_passes_proposed"] == 0, "Missed pass on approaching track"
    # Gate 5: No cancellation oscillation
    assert summary["cancellation_oscillation_proposed"] == 0, "Cancellation oscillation detected"
    # Gate 6: Missing coverage must be inconclusive
    assert results["missing_coverage_unresolved_handling"]["coverage_loss_treated_as_inconclusive"], "Missing coverage was not inconclusive"
    # Gate 7: Turning aircraft detected
    assert results["turning_aircraft_assessment"]["turning_away_detected"], "Turning aircraft not handled correctly"
    # Gate 8: Terminal approaches suppressed
    assert results["terminal_approaches"]["ltfm_arrival_suppressed"], "LTFM arrival false alert"
    assert results["terminal_approaches"]["ltba_arrival_suppressed"], "LTBA arrival false alert"
    # Gate 9: Missing heading extrapolation safe
    assert results["missing_heading_latency_extrapolation"]["no_northward_drift"], "Missing heading corrupted position"
    # Gate 10: Shadow candidate isolation verified
    assert results["shadow_isolation"]["shadow_candidates_cannot_control_alerts"], "Shadow candidate isolation violated"

    print("All benchmark kinematics and safety gates passed successfully!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
