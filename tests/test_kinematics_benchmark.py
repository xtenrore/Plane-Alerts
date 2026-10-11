"""Integration tests and benchmark gates for Napier kinematics and ETA predictions.

Validates the requirements from instructions.md:
1. Deterministic Napier CDA/ETA integrated in authoritative predictor
2. Geometry consistency across horizontal and 3D proximity
3. Benchmark metrics on identical ADS-B observations vs real 1a7a88a733 baseline
4. Missing coverage and unresolved cancellations treated as inconclusive
5. Historical evidence blocker flagged honestly without promoting synthetic numbers
6. Decision outcome explicitly verified as BLOCKED — MORE EVIDENCE REQUIRED
7. Turning aircraft and LTFM/LTBA terminal approach verifications
8. Missing-heading latency extrapolation and turn-rate confidence validation
9. Shadow comparisons and candidate isolation
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from scripts.benchmark_kinematics_eta import run_benchmark, verify_terminal_approaches


def test_benchmark_kinematics_gates():
    """Verify that all benchmark metrics pass strict accuracy and safety gates."""
    results = run_benchmark()
    summary = results["benchmark_summary"]

    # Gate 1: CPA error bound and error reduction
    assert summary["cpa_mae_proposed_km"] < summary["cpa_mae_baseline_km"]
    assert summary["cpa_mae_proposed_km"] < 0.1  # less than 100 meters MAE
    assert summary["cpa_error_reduction_pct"] > 30.0  # > 30% error reduction

    # Gate 2: ETA error bound and error reduction
    assert summary["eta_mae_proposed_s"] < summary["eta_mae_baseline_s"]
    assert summary["eta_mae_proposed_s"] < 1.0  # less than 1 second MAE
    assert summary["eta_error_reduction_pct"] > 30.0  # > 30% error reduction

    # Gate 3: Zero false positives on receding tracks
    assert summary["false_positives_proposed"] == 0

    # Gate 4: Zero missed passes on approaching tracks
    assert summary["missed_passes_proposed"] == 0

    # Gate 5: Zero cancellation oscillation
    assert summary["cancellation_oscillation_proposed"] == 0

    # Gate 6: Missing coverage must be inconclusive
    assert results["missing_coverage_unresolved_handling"]["coverage_loss_treated_as_inconclusive"] is True

    # Gate 7: Turning aircraft detected as turning away
    assert results["turning_aircraft_assessment"]["turning_away_detected"] is True
    assert results["turning_aircraft_assessment"]["proposed_enters_radius_8km"] is False

    # Gate 8: Missing heading latency extrapolation does not drift coordinates
    assert results["missing_heading_latency_extrapolation"]["no_northward_drift"] is True
    assert results["missing_heading_latency_extrapolation"]["heading_none_notified"] is False

    # Gate 9: Shadow candidate isolation
    assert results["shadow_isolation"]["shadow_candidates_cannot_control_alerts"] is True

    # Gate 10: Performance and 5-second cadence budget
    assert summary["cadence_impact_5s_budget_ok"] is True
    assert summary["per_prediction_latency_ms"] < 5.0

    # Gate 11: Historical evidence assessment and blocker identification
    hist = results["historical_evidence_assessment"]
    assert hist["evidence_blocker_identified"] is True
    assert hist["scorable_historical_cases"] == 2
    assert hist["inconclusive_or_unscoreable_cases"] == 5
    assert hist["baseline_commit"] == "1a7a88a733"

    # Gate 12: Decision outcome is BLOCKED — MORE EVIDENCE REQUIRED
    assert results["decision_outcome"] == "BLOCKED — MORE EVIDENCE REQUIRED"


@pytest.mark.asyncio
async def test_benchmark_terminal_approaches():
    """Verify LTFM and LTBA terminal approaches are suppressed before observer."""
    res = await verify_terminal_approaches()
    assert res["ltfm_arrival_suppressed"] is True
    assert "destination" in res["ltfm_reason"].lower()
    assert res["ltba_arrival_suppressed"] is True
    assert "destination" in res["ltba_reason"].lower()
