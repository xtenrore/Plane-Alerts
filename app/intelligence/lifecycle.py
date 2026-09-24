"""Pure alert lifecycle decisions for Plane Alerts."""
from __future__ import annotations

from dataclasses import dataclass

CANCELLATION_CONFIRMATIONS_REQUIRED = 3
PHOTO_NOW_EXIT_HYSTERESIS_S = 30.0


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


def stabilize_lifecycle_stage(
    previous_stage: str | None,
    proposed_stage: str,
    best_start_s: float | None,
) -> str:
    """Prevent PHOTO NOW / CAMERA READY threshold chatter without hiding turns.

    retired external agent seq142 showed a single live message flipping between these two stages
    repeatedly as the predicted shooting-window start jittered around zero. A
    small *exit* band is presentation hysteresis only: physical CPA/ETA is left
    untouched, and a genuinely moved window (>30 s away) may still fall back to
    CAMERA READY. Other stage transitions remain unchanged.
    """
    previous = str(previous_stage or "")
    proposed = str(proposed_stage or "")
    if previous != "photo_now" or proposed != "camera_ready":
        return proposed
    try:
        start = float(best_start_s) if best_start_s is not None else None
    except (TypeError, ValueError):
        return proposed
    if start is not None and 0.0 < start <= PHOTO_NOW_EXIT_HYSTERESIS_S:
        return "photo_now"
    return proposed


def cancelled_latch_allows_reactivation(
    previous_stage: str | None,
    previous_active: bool,
    prediction,
    fresh_observation: bool,
    alert_radius_km: float,
) -> bool:
    """Allow cancelled encounters back only on fresh direct physical presence.

    A confirmed cancellation must not be undone by a later predictive qualify
    cycle. That was the seq143 oscillation class. A permanent strict latch is
    also unsafe, however: if the earlier cancellation was wrong and a fresh
    aircraft position is physically inside the user's radius, that observation
    is stronger evidence and may recover the encounter.
    """
    if bool(previous_active) or str(previous_stage or "") != "cancelled":
        return True
    if not fresh_observation or bool(getattr(prediction, "stale", False)):
        return False
    try:
        current = float(getattr(prediction, "current_distance_km"))
        radius = float(alert_radius_km)
    except (TypeError, ValueError):
        return False
    return current <= radius


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
    """Return whether *this cycle* contains credible cancellation evidence."""
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
    """Accumulate credible cancellation evidence without provider-gap starvation."""
    count = max(0, int(previous_count))
    if not candidate:
        return False, count
    count += 1
    return count >= max(1, int(required)), count
