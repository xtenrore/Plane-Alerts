"""Pure alert lifecycle decisions for Plane Alerts."""
from __future__ import annotations

from dataclasses import dataclass

CANCELLATION_CONFIRMATIONS_REQUIRED = 3


@dataclass(frozen=True)
class LifecycleDecision:
    stage: str
    should_have_live_message: bool
    reason: str


def decide_lifecycle(prediction, best_start_s: float | None = None, best_end_s: float | None = None) -> LifecycleDecision:
    if getattr(prediction, "stale", False):
        return LifecycleDecision("uncertain", False, "ADS-B position is stale")
    if getattr(prediction, "already_passed", False) or getattr(prediction, "state", "") == "Passed":
        return LifecycleDecision("passed", True, "closest approach has occurred")
    if not getattr(prediction, "enters_alert_radius", False):
        return LifecycleDecision("detection", False, "projected CPA remains outside alert radius")
    cpa = getattr(prediction, "time_to_cpa_s", None)
    start = best_start_s if best_start_s is not None else (max(0.0, cpa - 25.0) if cpa is not None else None)
    end = best_end_s if best_end_s is not None else ((cpa + 10.0) if cpa is not None else None)
    if start is None:
        return LifecycleDecision("candidate", False, "trajectory qualifies but shooting window is uncertain")
    if start <= 0 <= (end if end is not None else 0):
        return LifecycleDecision("photo_now", True, "best shooting window is active")
    if start <= 120:
        return LifecycleDecision("camera_ready", True, "shooting window is within two minutes")
    if start <= 300:
        return LifecycleDecision("prepare", True, "shooting window is within five minutes")
    return LifecycleDecision("candidate", False, "trajectory qualifies but preparation is not yet useful")


def prediction_changed(previous_cpa_km: float | None, new_cpa_km: float, alert_radius_km: float) -> bool:
    """Flag only a material recalculation, not ordinary CPA jitter."""
    if previous_cpa_km is None:
        return False
    previous = float(previous_cpa_km)
    delta = abs(float(new_cpa_km) - previous)
    threshold = max(3.0, float(alert_radius_km) * 0.35, abs(previous) * 0.50)
    return delta >= threshold


def should_finalize_observed_pass(
    prediction,
    observed_closest_km: float | None,
    alert_radius_km: float,
) -> bool:
    """Return whether an active alert has *observably* completed its pass.

    A projected CPA inside the radius is not ground truth.  We only finalize a
    pass after Plane Alerts has actually observed the aircraft inside the user's
    radius and a later fresh trajectory shows it receding from that observed
    minimum.  This prevents close-looking predictions that later turn away from
    being mislabeled as successful passes, while also preventing a genuine
    observed pass from being emitted as a cancellation after route/CPA evidence
    changes.

    Missing/stale ADS-B remains unresolved: stale observations never finalize a
    pass.
    """
    if getattr(prediction, "stale", False) or observed_closest_km is None:
        return False
    try:
        radius = float(alert_radius_km)
        observed = float(observed_closest_km)
        current = float(getattr(prediction, "current_distance_km"))
    except (TypeError, ValueError):
        return False
    if observed > radius:
        return False

    # Require real separation from the recorded minimum, not ordinary provider
    # jitter.  The floor is deliberately small so a low/slow nearby aircraft is
    # not kept active for an excessive period after its closest point.
    receded_km = current - observed
    if receded_km < max(0.15, radius * 0.01):
        return False

    trend = getattr(prediction, "distance_trend_km_s", None)
    try:
        moving_away = trend is not None and float(trend) >= 0.002
    except (TypeError, ValueError):
        moving_away = False
    state = str(getattr(prediction, "state", "") or "")
    return bool(
        moving_away
        or state in {"Moving away", "Turning away", "Passed"}
        or getattr(prediction, "already_passed", False)
    )


def should_cancel_active_alert(prediction, previous_cpa_km: float | None, alert_radius_km: float) -> bool:
    """Return whether *this cycle* contains credible cancellation evidence.

    The caller still has to observe this evidence multiple times. Provider gaps or
    low-confidence miss samples do not themselves become cancellation evidence.
    A sustained observed turn-away/moving-away signal is itself cancellation
    evidence even when that manoeuvre lowers the predictor confidence score.
    """
    if getattr(prediction, "stale", False):
        return False
    if getattr(prediction, "already_passed", False) or getattr(prediction, "state", "") == "Passed":
        return False
    if getattr(prediction, "enters_alert_radius", False):
        return False

    radius = float(alert_radius_km)
    new_cpa = float(getattr(prediction, "projected_closest_km", radius))
    hysteresis_km = max(2.0, radius * 0.18)
    if new_cpa <= radius + hysteresis_km:
        return False

    previous = float(previous_cpa_km) if previous_cpa_km is not None else None
    materially_worse = previous is None or new_cpa - previous >= max(2.5, radius * 0.22)
    if not materially_worse:
        return False

    trend = getattr(prediction, "distance_trend_km_s", None)
    moving_away = trend is not None and float(trend) >= 0.004
    turning_away = bool(getattr(prediction, "turning_away", False)) or str(getattr(prediction, "state", "")) == "Turning away"
    confidence_score = float(getattr(prediction, "confidence_score", 1.0) if getattr(prediction, "confidence_score", None) is not None else 1.0)

    # Turning aircraft are intentionally penalized by the confidence model. Do
    # not let that penalty freeze cancellation forever when the physical
    # evidence already says the aircraft is moving/turning away. The monitor
    # still requires three independent confirmations before cancelling.
    if moving_away or turning_away:
        return True

    state = str(getattr(prediction, "state", ""))
    clear_miss_margin_km = max(5.0, radius * 0.50)
    clear_confident_miss = (
        state == "Will not approach"
        and new_cpa >= radius + clear_miss_margin_km
        and confidence_score >= 0.58
    )

    extreme_miss_margin_km = max(10.0, radius * 1.00)
    extreme_repeated_miss = (
        state == "Will not approach"
        and new_cpa >= radius + extreme_miss_margin_km
        and confidence_score >= 0.36
    )

    return clear_confident_miss or extreme_repeated_miss


def advance_cancellation_confirmation(previous_count: int, candidate: bool, *, required: int = CANCELLATION_CONFIRMATIONS_REQUIRED) -> tuple[bool, int]:
    """Accumulate credible cancellation evidence without provider-gap starvation.

    A non-evidence cycle while the alert is already in the non-qualifying branch is
    neutral: it neither increments nor erases prior credible evidence. This matters
    when fresh provider samples alternate with degraded/low-confidence continuity
    samples. A genuinely qualifying trajectory is handled outside this branch by the
    monitor and resets the stored cancellation counter when the stable ETA is updated.
    """
    count = max(0, int(previous_count))
    if not candidate:
        return False, count
    count += 1
    return count >= max(1, int(required)), count
