"""Plane Alerts v6.0 ETA/CPA diagnostic attribution and age-aware uncertainty safeguards.

Rules:
- First identify whether each ETA/CPA discrepancy is caused by old observations,
  a changing trajectory, airport-path handling, or delivery timing.
- Preserve the published v5.3 age-aware midpoint ETA behavior
  (PREDICTION_VERSION = '5.3-3d-proximity-age-aware') and existing confidence /
  uncertainty safeguards unless fresh evidence proves a specific defect.
- Measure ETA error ONLY for events with a reliable observed pass; mark uncovered
  or stale-coverage cases inconclusive.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Iterable, Sequence

from app.intelligence import trajectory as traj
from app.intelligence.prediction_v46 import freshness_limit_s, position_uncertainty_km

ETA_CPA_V6_VERSION = "6.0-age-aware-eta-cpa-attribution-v1"


@dataclass(slots=True, frozen=True)
class EtaCpaDiagnosis:
    """Attribution and uncertainty envelope for a live ETA/CPA prediction."""

    primary_driver: str  # "OLD_OBSERVATIONS", "CHANGING_TRAJECTORY", "AIRPORT_PATH_HANDLING", "DELIVERY_TIMING", "STABLE_NOMINAL"
    reason_code: str
    explanation: str
    effective_age_s: float
    freshness_limit_s: float
    age_corrected_time_to_cpa_s: float | None
    age_corrected_radius_entry_s: float | None
    eta_uncertainty_s: float | None
    cpa_uncertainty_km: float | None
    turn_rate_deg_s: float
    acceleration_kts_s: float
    delivery_lag_s: float
    eta_available: bool
    eta_reliable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_driver": self.primary_driver,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "effective_age_s": self.effective_age_s,
            "freshness_limit_s": self.freshness_limit_s,
            "age_corrected_time_to_cpa_s": self.age_corrected_time_to_cpa_s,
            "age_corrected_radius_entry_s": self.age_corrected_radius_entry_s,
            "eta_uncertainty_s": self.eta_uncertainty_s,
            "cpa_uncertainty_km": self.cpa_uncertainty_km,
            "turn_rate_deg_s": self.turn_rate_deg_s,
            "acceleration_kts_s": self.acceleration_kts_s,
            "delivery_lag_s": self.delivery_lag_s,
            "eta_available": self.eta_available,
            "eta_reliable": self.eta_reliable,
        }


@dataclass(slots=True, frozen=True)
class EtaOutcomeEvaluation:
    """Evaluation of predicted ETA against a fresh observed pass or inconclusive coverage."""

    status: str  # "scored_observed_pass", "inconclusive_missing_coverage", "inconclusive_no_observed_pass", "inconclusive_eta_unavailable"
    scoreable: bool
    predicted_eta_s: float | None
    observed_time_to_closest_s: float | None
    signed_eta_error_s: float | None
    absolute_eta_error_s: float | None
    cpa_error_km: float | None
    attributed_cause: str
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "scoreable": self.scoreable,
            "predicted_eta_s": self.predicted_eta_s,
            "observed_time_to_closest_s": self.observed_time_to_closest_s,
            "signed_eta_error_s": self.signed_eta_error_s,
            "absolute_eta_error_s": self.absolute_eta_error_s,
            "cpa_error_km": self.cpa_error_km,
            "attributed_cause": self.attributed_cause,
            "explanation": self.explanation,
        }


def _finite(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def refine_age_aware_radius_entry_s(
    path: Sequence[Any],
    alert_radius_km: float,
    *,
    fallback_entry_s: float | None = None,
) -> float | None:
    """Refine radius_entry_s using linear sub-step interpolation between path points.

    The v4.3/v5.3 predictor shifts path point timestamps by `age` and picks the
    first 3-second grid point inside `alert_radius_km`. Sub-step linear interpolation
    across the boundary crossing removes up to 3 seconds of grid quantization bias
    without changing CPA geometry or qualification membership.
    """
    pts = list(path or [])
    if not pts:
        return fallback_entry_s
    radius = float(alert_radius_km)
    first_h = _finite(getattr(pts[0], "horizontal_km", None))
    first_t = _finite(getattr(pts[0], "seconds", None))
    if first_h is not None and first_h <= radius:
        return max(0.0, float(first_t or 0.0))

    for prev_pt, curr_pt in zip(pts, pts[1:]):
        h0 = _finite(getattr(prev_pt, "horizontal_km", None))
        h1 = _finite(getattr(curr_pt, "horizontal_km", None))
        t0 = _finite(getattr(prev_pt, "seconds", None))
        t1 = _finite(getattr(curr_pt, "seconds", None))
        if None in (h0, h1, t0, t1):
            continue
        if float(h0) > radius >= float(h1):
            dh = float(h0) - float(h1)
            dt = float(t1) - float(t0)
            if dh > 1e-6 and dt >= 0.0:
                frac = max(0.0, min(1.0, (float(h0) - radius) / dh))
                return round(max(0.0, float(t0) + frac * dt), 3)
            return round(max(0.0, float(t1)), 3)
    return fallback_entry_s


def diagnose_eta_cpa(
    *,
    prediction: Any,
    samples: Iterable[traj.HistorySample],
    alert_radius_km: float,
    now: float | None = None,
    airport_classification: str = "NONE",
    airport_alert_changed: bool = False,
    delivery_lag_s: float = 0.0,
    initial_confirmations_pending: int = 0,
) -> EtaCpaDiagnosis:
    """Diagnose the primary driver of ETA/CPA uncertainty or shift without altering the predictor."""
    ordered = sorted(list(samples), key=lambda s: s.timestamp)
    effective_now = time.time() if now is None else float(now)
    if not ordered:
        return EtaCpaDiagnosis(
            primary_driver="OLD_OBSERVATIONS",
            reason_code="ETA_UNAVAILABLE_NO_SAMPLES",
            explanation="No ADS-B history samples available; ETA and CPA cannot be estimated.",
            effective_age_s=math.inf,
            freshness_limit_s=12.0,
            age_corrected_time_to_cpa_s=None,
            age_corrected_radius_entry_s=None,
            eta_uncertainty_s=None,
            cpa_uncertainty_km=None,
            turn_rate_deg_s=0.0,
            acceleration_kts_s=0.0,
            delivery_lag_s=float(delivery_lag_s),
            eta_available=False,
            eta_reliable=False,
        )

    latest = ordered[-1]
    effective_age = max(float(latest.position_age_s or 0.0), max(0.0, effective_now - float(latest.timestamp)))
    speed_kts = _finite(latest.speed_kts) or 250.0
    limit_s = freshness_limit_s(speed_kts)
    pos_unc_km = position_uncertainty_km(ordered, now=effective_now)
    cpa_t = _finite(getattr(prediction, "time_to_cpa_s", None))
    raw_entry_t = _finite(getattr(prediction, "radius_entry_s", None))
    path = list(getattr(prediction, "path", None) or [])
    refined_entry_t = refine_age_aware_radius_entry_s(path, alert_radius_km, fallback_entry_s=raw_entry_t)

    turn_rate = float(_finite(getattr(prediction, "turn_rate_deg_s", 0.0)) or 0.0)
    accel = float(_finite(getattr(prediction, "acceleration_kts_s", 0.0)) or 0.0)
    h_spread = _finite(getattr(prediction, "heading_stability_deg", None))
    s_spread = _finite(getattr(prediction, "speed_stability_kts", None))
    stale = bool(getattr(prediction, "stale", False)) or effective_age > limit_s
    conf_label = str(getattr(prediction, "confidence", "Uncertain") or "Uncertain")

    speed_km_s = max(0.03, float(speed_kts) * traj.KNOTS_TO_KM_S)
    horizon_s = float(cpa_t or 0.0)
    # Uncertainty grows with position uncertainty, turn rate, speed spread, and horizon
    turn_cross_km = (horizon_s / 60.0) * abs(turn_rate) * 0.45
    cpa_unc_km = round(pos_unc_km + turn_cross_km, 3)
    eta_unc_s = (
        round(
            max(
                3.0,
                (pos_unc_km / speed_km_s)
                + (0.08 * horizon_s if abs(turn_rate) >= 0.25 else 0.03 * horizon_s)
                + (0.12 * horizon_s if (s_spread is not None and s_spread > 20.0) else 0.0),
            ),
            2,
        )
        if cpa_t is not None and not stale
        else None
    )

    eta_available = cpa_t is not None and not stale
    eta_reliable = bool(
        eta_available
        and conf_label in {"High", "Medium", "Low"}
        and len(ordered) >= 3
        and (eta_unc_s is not None and eta_unc_s <= max(45.0, horizon_s * 0.35))
    )

    # Identify primary driver in priority order:
    if stale or effective_age >= max(8.0, limit_s * 0.70):
        driver = "OLD_OBSERVATIONS"
        code = "ETA_DRIVER_OBSERVATION_AGE"
        explanation = (
            f"Provider/observation age is {effective_age:.1f}s (freshness limit {limit_s:.1f}s); "
            "age-aware midpoint ETA compensation applied."
        )
    elif airport_alert_changed or airport_classification in {"ARRIVAL", "HOLDING", "MISSED_APPROACH"}:
        driver = "AIRPORT_PATH_HANDLING"
        code = f"ETA_DRIVER_AIRPORT_{airport_classification}"
        explanation = (
            f"Terminal airport path handling ({airport_classification}) governs expected trajectory "
            "and overrides straight-line cruise extrapolation."
        )
    elif abs(turn_rate) >= 0.20 or abs(accel) >= 0.60 or (h_spread is not None and h_spread >= 12.0):
        driver = "CHANGING_TRAJECTORY"
        code = "ETA_DRIVER_MANEUVERING_TRACK"
        explanation = (
            f"Aircraft trajectory is actively changing (turn_rate={turn_rate:+.2f} deg/s, "
            f"accel={accel:+.2f} kts/s); decaying midpoint integration bounds extrapolation."
        )
    elif float(delivery_lag_s) >= 4.0 or int(initial_confirmations_pending) > 0:
        driver = "DELIVERY_TIMING"
        code = "ETA_DRIVER_CONFIRMATION_OR_DELIVERY_TIMING"
        explanation = (
            f"Notification timing reflects initial confirmation gating "
            f"(pending={initial_confirmations_pending}) or delivery latency ({delivery_lag_s:.1f}s)."
        )
    else:
        driver = "STABLE_NOMINAL"
        code = "ETA_DRIVER_STABLE_MIDPOINT_TRACK"
        explanation = "Fresh multi-sample track with stable heading and groundspeed; age-aware midpoint ETA is nominal."

    return EtaCpaDiagnosis(
        primary_driver=driver,
        reason_code=code,
        explanation=explanation,
        effective_age_s=round(effective_age, 2),
        freshness_limit_s=round(limit_s, 2),
        age_corrected_time_to_cpa_s=round(cpa_t, 2) if cpa_t is not None else None,
        age_corrected_radius_entry_s=refined_entry_t,
        eta_uncertainty_s=eta_unc_s,
        cpa_uncertainty_km=cpa_unc_km,
        turn_rate_deg_s=round(turn_rate, 3),
        acceleration_kts_s=round(accel, 3),
        delivery_lag_s=round(float(delivery_lag_s), 2),
        eta_available=eta_available,
        eta_reliable=eta_reliable,
    )


def evaluate_confirmed_pass_eta_error(
    *,
    predicted_at_epoch_s: float,
    predicted_time_to_cpa_s: float | None,
    predicted_cpa_km: float | None,
    observed_pass: bool,
    coverage_missing_or_stale: bool,
    observed_closest_epoch_s: float | None = None,
    observed_closest_km: float | None = None,
    alert_radius_km: float = 10.0,
    diagnosis: EtaCpaDiagnosis | None = None,
) -> EtaOutcomeEvaluation:
    """Measure ETA error strictly for events with a reliable observed pass.

    Uncovered or unconfirmed passes are explicitly marked inconclusive and
    excluded from scored ETA error numerators/denominators without being dropped.
    """
    if coverage_missing_or_stale:
        return EtaOutcomeEvaluation(
            status="inconclusive_missing_coverage",
            scoreable=False,
            predicted_eta_s=predicted_time_to_cpa_s,
            observed_time_to_closest_s=None,
            signed_eta_error_s=None,
            absolute_eta_error_s=None,
            cpa_error_km=None,
            attributed_cause="OLD_OBSERVATIONS",
            explanation="ADS-B coverage was missing or stale during pass window; outcome is inconclusive.",
        )

    if not observed_pass or observed_closest_epoch_s is None or observed_closest_km is None or float(observed_closest_km) > float(alert_radius_km):
        return EtaOutcomeEvaluation(
            status="inconclusive_no_observed_pass",
            scoreable=False,
            predicted_eta_s=predicted_time_to_cpa_s,
            observed_time_to_closest_s=None,
            signed_eta_error_s=None,
            absolute_eta_error_s=None,
            cpa_error_km=None,
            attributed_cause=diagnosis.primary_driver if diagnosis else "CHANGING_TRAJECTORY",
            explanation="No confirmed observed in-radius pass occurred; ETA error is not scored on non-pass events.",
        )

    if predicted_time_to_cpa_s is None or not math.isfinite(float(predicted_time_to_cpa_s)):
        return EtaOutcomeEvaluation(
            status="inconclusive_eta_unavailable",
            scoreable=False,
            predicted_eta_s=None,
            observed_time_to_closest_s=max(0.0, float(observed_closest_epoch_s) - float(predicted_at_epoch_s)),
            signed_eta_error_s=None,
            absolute_eta_error_s=None,
            cpa_error_km=None,
            attributed_cause=diagnosis.primary_driver if diagnosis else "OLD_OBSERVATIONS",
            explanation="Pass was observed, but ETA was unavailable/uncertain at snapshot time.",
        )

    actual_dt_s = max(0.0, float(observed_closest_epoch_s) - float(predicted_at_epoch_s))
    pred_eta_s = float(predicted_time_to_cpa_s)
    signed_err_s = round(pred_eta_s - actual_dt_s, 2)
    abs_err_s = round(abs(signed_err_s), 2)
    cpa_err_km = (
        round(abs(float(predicted_cpa_km) - float(observed_closest_km)), 3)
        if predicted_cpa_km is not None
        else None
    )
    cause = diagnosis.primary_driver if diagnosis else "STABLE_NOMINAL"
    return EtaOutcomeEvaluation(
        status="scored_observed_pass",
        scoreable=True,
        predicted_eta_s=round(pred_eta_s, 2),
        observed_time_to_closest_s=round(actual_dt_s, 2),
        signed_eta_error_s=signed_err_s,
        absolute_eta_error_s=abs_err_s,
        cpa_error_km=cpa_err_km,
        attributed_cause=cause,
        explanation=(
            f"Scored against confirmed observed in-radius pass ({observed_closest_km:.2f} km): "
            f"predicted ETA {pred_eta_s:.1f}s vs observed {actual_dt_s:.1f}s (error {signed_err_s:+.1f}s)."
        ),
    )
