"""Plane Alerts v4.6 prediction confidence, uncertainty, and shadow models.

The production CPA geometry remains the proven v4.4/v4.5 predictor.  This layer
adds deterministic freshness/uncertainty/confidence evidence and records bounded
linear/turn-aware candidates for shadow evaluation.  Shadow candidates never
control alerts.
"""
from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from statistics import median, pstdev
from typing import Any, Iterable

from app.intelligence import trajectory as t

PRODUCTION_MODEL_VERSION = "4.6-confidence-freshness"
SHADOW_MODEL_VERSION = "4.6-turn-shadow-1"

_MIN_FRESHNESS_S = 8.0
_MAX_FRESHNESS_S = 30.0
_POSITION_DRIFT_BUDGET_KM = 2.5
_TURN_MIN_RATES = 3
_TURN_MIN_SPAN_S = 10.0
_TURN_MAX_GAP_S = 18.0
_TURN_MIN_RATE_DEG_S = 0.06
_TURN_MAX_RATE_DEG_S = 2.2
_TURN_MIN_SIGN_AGREEMENT = 0.75
_TURN_MAX_RATE_SPREAD = 0.32
_SHADOW_MAX_HORIZON_S = 600
_DIAGNOSTIC_TTL_S = 180.0
_DIAGNOSTIC_MAX = 4096

_BASE_PREDICT = t.predict_trajectory
_INSTALLED = False
_diagnostics: "OrderedDict[int, tuple[float, dict[str, Any]]]" = OrderedDict()


@dataclass(slots=True, frozen=True)
class TurnEvidence:
    stable: bool
    rate_deg_s: float
    sample_rates: int
    sign_agreement: float
    rate_spread_deg_s: float | None
    span_s: float
    reason: str


@dataclass(slots=True, frozen=True)
class ShadowResult:
    model_version: str
    mode: str
    cpa_km: float
    eta_s: float | None
    enters_radius: bool
    used_turn: bool
    turn_rate_deg_s: float
    horizon_s: int


@dataclass(slots=True, frozen=True)
class ConfidenceEvidence:
    score: float
    label: str
    observation_age_s: float
    freshness_limit_s: float
    position_uncertainty_km: float
    observation_count: int
    observation_span_s: float
    update_interval_median_s: float | None
    update_interval_spread_s: float | None
    heading_stability_deg: float | None
    speed_stability_kts: float | None
    turn_stability: str
    acceleration_kts_s: float
    distance_trend_available: bool
    prediction_horizon_s: float | None
    missing_fields: tuple[str, ...]
    ground_evidence: bool
    reasons: tuple[str, ...]


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _angle_delta(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


def _sample_age(sample: t.HistorySample, now: float) -> float:
    return max(float(sample.position_age_s or 0.0), max(0.0, float(now) - float(sample.timestamp)))


def _usable_speed_kts(samples: list[t.HistorySample]) -> float:
    values = [
        float(item.speed_kts)
        for item in samples[-7:]
        if item.speed_kts is not None and 0.0 <= float(item.speed_kts) <= 1400.0
    ]
    if values:
        return median(values)
    return 0.0


def freshness_limit_s(speed_kts: float | None) -> float:
    """Return a bounded observation-age limit based on possible travel distance."""
    speed = max(0.0, float(speed_kts or 0.0))
    speed_km_s = speed * t.KNOTS_TO_KM_S
    if speed_km_s <= 0.005:
        return _MAX_FRESHNESS_S
    limit = 3.0 + (_POSITION_DRIFT_BUDGET_KM / speed_km_s)
    return _clamp(limit, _MIN_FRESHNESS_S, _MAX_FRESHNESS_S)


def _interval_stats(samples: list[t.HistorySample]) -> tuple[float | None, float | None]:
    intervals = [
        b.timestamp - a.timestamp
        for a, b in zip(samples, samples[1:])
        if 0.5 <= b.timestamp - a.timestamp <= 30.0
    ]
    if not intervals:
        return None, None
    return median(intervals), pstdev(intervals) if len(intervals) >= 2 else 0.0


def _irregular_observation_timing(samples: list[t.HistorySample]) -> bool:
    """Recognize cadence gaps without allowing those gaps into the turn model."""
    gaps = [
        float(b.timestamp) - float(a.timestamp)
        for a, b in zip(samples, samples[1:])
        if 0.5 <= float(b.timestamp) - float(a.timestamp) <= 60.0
    ]
    if len(gaps) < 2:
        return False
    return max(gaps) > 22.0 or pstdev(gaps) > 5.0


def position_uncertainty_km(samples: Iterable[t.HistorySample], *, now: float) -> float:
    ordered = sorted(list(samples), key=lambda item: item.timestamp)
    if not ordered:
        return math.inf
    speed = _usable_speed_kts(ordered)
    speed_km_s = speed * t.KNOTS_TO_KM_S
    age = _sample_age(ordered[-1], now)
    interval_median, interval_spread = _interval_stats(ordered[-10:])
    cadence_allowance = min(8.0, float(interval_median or 0.0) * 0.25)
    irregularity = min(8.0, float(interval_spread or 0.0))
    # 150 m floor covers ADS-B quantization/provider merging without pretending
    # this is a calibrated navigation-error ellipse.
    return 0.15 + speed_km_s * (age + cadence_allowance + irregularity * 0.5)


def turn_evidence(samples: Iterable[t.HistorySample], *, now: float | None = None) -> TurnEvidence:
    ordered = sorted(list(samples), key=lambda item: item.timestamp)
    if len(ordered) < 4:
        return TurnEvidence(False, 0.0, 0, 0.0, None, 0.0, "fewer than four observations")

    effective_now = time.time() if now is None else float(now)
    latest = ordered[-1]
    speed = _usable_speed_kts(ordered)
    if _sample_age(latest, effective_now) > freshness_limit_s(speed):
        return TurnEvidence(False, 0.0, 0, 0.0, None, 0.0, "latest observation is not fresh enough")

    rates: list[float] = []
    first_t: float | None = None
    last_t: float | None = None
    usable = [item for item in ordered[-10:] if item.heading_deg is not None]
    for left, right in zip(usable, usable[1:]):
        dt = float(right.timestamp) - float(left.timestamp)
        if not 0.75 <= dt <= _TURN_MAX_GAP_S:
            continue
        rate = _angle_delta(float(right.heading_deg), float(left.heading_deg)) / dt
        if abs(rate) > _TURN_MAX_RATE_DEG_S:
            continue
        if first_t is None:
            first_t = float(left.timestamp)
        last_t = float(right.timestamp)
        rates.append(rate)

    span = max(0.0, (last_t or 0.0) - (first_t or 0.0))
    meaningful = [rate for rate in rates if abs(rate) >= _TURN_MIN_RATE_DEG_S]
    if len(meaningful) < _TURN_MIN_RATES or span < _TURN_MIN_SPAN_S:
        return TurnEvidence(False, 0.0, len(meaningful), 0.0, None, span, "turn history is too short or weak")

    positive = sum(rate > 0 for rate in meaningful)
    negative = len(meaningful) - positive
    sign_agreement = max(positive, negative) / len(meaningful)
    direction = 1.0 if positive >= negative else -1.0
    directed = [abs(rate) for rate in meaningful if math.copysign(1.0, rate) == direction]
    rate = direction * median(directed)
    spread = pstdev(directed) if len(directed) >= 2 else 0.0

    if sign_agreement < _TURN_MIN_SIGN_AGREEMENT:
        return TurnEvidence(False, rate, len(meaningful), sign_agreement, spread, span, "turn direction is inconsistent")
    if spread > _TURN_MAX_RATE_SPREAD:
        return TurnEvidence(False, rate, len(meaningful), sign_agreement, spread, span, "turn rate is unstable")
    return TurnEvidence(True, _clamp(rate, -1.8, 1.8), len(meaningful), sign_agreement, spread, span, "sustained consistent turn")


def _motion_state(samples: list[t.HistorySample]) -> tuple[float | None, float | None]:
    # Reuse the proven robust estimators without changing their production use.
    return t._estimate_speed_heading(samples)  # type: ignore[attr-defined]


def _shadow_simulation(
    samples: Iterable[t.HistorySample],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    *,
    now: float,
    use_turn: bool,
    evidence: TurnEvidence,
    max_horizon_s: int = _SHADOW_MAX_HORIZON_S,
    step_s: int = 3,
) -> ShadowResult:
    ordered = t._contiguous_recent(sorted(list(samples), key=lambda item: item.timestamp))  # type: ignore[attr-defined]
    if not ordered:
        return ShadowResult(SHADOW_MODEL_VERSION, "turn-aware" if use_turn else "linear", math.inf, None, False, False, 0.0, 0)

    speed, heading = _motion_state(ordered)
    current = t.haversine_km(ordered[-1].latitude, ordered[-1].longitude, user_lat, user_lon)
    if speed is None or heading is None or speed < 20:
        return ShadowResult(SHADOW_MODEL_VERSION, "turn-aware" if use_turn else "linear", current, None, current <= alert_radius_km, False, 0.0, 0)

    speed_km_s = float(speed) * t.KNOTS_TO_KM_S
    dynamic = int(current / max(speed_km_s, 1e-6) * 1.25 + 45)
    horizon = min(int(max_horizon_s), max(120, dynamic))
    age = _sample_age(ordered[-1], now)
    if age > freshness_limit_s(speed):
        return ShadowResult(SHADOW_MODEL_VERSION, "turn-aware" if use_turn else "linear", current, None, False, False, 0.0, horizon)

    lat = float(ordered[-1].latitude)
    lon = float(ordered[-1].longitude)
    hdg = float(heading) % 360.0
    closest = current
    eta = 0.0
    turn_rate = evidence.rate_deg_s if use_turn and evidence.stable else 0.0
    used_turn = bool(use_turn and evidence.stable)
    for seconds in range(step_s, horizon + 1, step_s):
        # Candidate turn extrapolation is deliberately bounded: strong recent
        # curvature fades to straight flight instead of circling indefinitely.
        if used_turn:
            decay = max(0.0, 1.0 - (seconds - step_s) / 90.0)
            midpoint_heading = (hdg + turn_rate * decay * step_s * 0.5) % 360.0
            lat, lon = t.project_point(lat, lon, speed_km_s * step_s, midpoint_heading)
            hdg = (hdg + turn_rate * decay * step_s) % 360.0
        else:
            lat, lon = t.project_point(lat, lon, speed_km_s * step_s, hdg)
        distance = t.haversine_km(lat, lon, user_lat, user_lon)
        if distance < closest:
            closest = distance
            eta = float(seconds)
    eta = max(0.0, eta - age)
    return ShadowResult(
        SHADOW_MODEL_VERSION,
        "turn-aware" if use_turn else "linear",
        closest,
        eta,
        closest <= alert_radius_km and eta > 0.0,
        used_turn,
        turn_rate,
        horizon,
    )


def _ground_evidence(samples: list[t.HistorySample]) -> bool:
    recent = samples[-5:]
    if len(recent) < 3:
        return False
    usable = [item for item in recent if item.altitude_m is not None and item.speed_kts is not None]
    if len(usable) < 3:
        return False
    low_alt = sum(float(item.altitude_m) <= 250.0 for item in usable) >= 3
    slow = sum(float(item.speed_kts) <= 55.0 for item in usable) >= 3
    not_climbing = sum(abs(float(item.vertical_rate_mps or 0.0)) <= 1.5 for item in usable) >= 3
    if not (low_alt and slow and not_climbing):
        return False
    movement = t.haversine_km(
        usable[0].latitude, usable[0].longitude, usable[-1].latitude, usable[-1].longitude
    )
    span = max(1.0, usable[-1].timestamp - usable[0].timestamp)
    implied_kts = movement / span / t.KNOTS_TO_KM_S
    return implied_kts <= 65.0


def _confidence_evidence(
    samples: list[t.HistorySample],
    prediction: t.TrajectoryPrediction,
    *,
    now: float,
    raw_samples: list[t.HistorySample] | None = None,
) -> ConfidenceEvidence:
    latest = samples[-1]
    timing_samples = (raw_samples or samples)[-10:]
    age = _sample_age(latest, now)
    speed = _usable_speed_kts(samples)
    freshness = freshness_limit_s(speed)
    uncertainty = position_uncertainty_km(timing_samples, now=now)
    interval_median, interval_spread = _interval_stats(timing_samples)
    irregular_timing = _irregular_observation_timing(timing_samples)
    heading_spread = prediction.heading_stability_deg
    speed_spread = prediction.speed_stability_kts
    turn = turn_evidence(samples, now=now)
    span = max(0.0, samples[-1].timestamp - samples[0].timestamp) if len(samples) >= 2 else 0.0

    missing: list[str] = []
    for name, value in (
        ("speed", latest.speed_kts),
        ("heading", latest.heading_deg),
        ("altitude", latest.altitude_m),
        ("vertical_rate", latest.vertical_rate_mps),
    ):
        if value is None:
            missing.append(name)

    freshness_component = 1.0 - _clamp(age / max(freshness, 1.0), 0.0, 1.0)
    observation_component = _clamp((len(samples) - 1) / 7.0, 0.0, 1.0)
    timing_component = 0.55 if interval_median is None else 1.0 - _clamp(float(interval_spread or 0.0) / 7.0, 0.0, 0.8)
    if irregular_timing:
        timing_component = min(timing_component, 0.30)
    heading_component = 0.45 if heading_spread is None else 1.0 - _clamp(float(heading_spread) / 25.0, 0.0, 1.0)
    speed_component = 0.55 if speed_spread is None else 1.0 - _clamp(float(speed_spread) / 45.0, 0.0, 1.0)
    turn_component = 0.85 if abs(float(prediction.turn_rate_deg_s or 0.0)) < 0.06 else (1.0 if turn.stable else 0.35)
    trend_component = 1.0 if prediction.distance_trend_km_s is not None else 0.45
    horizon = prediction.time_to_cpa_s
    horizon_component = 0.55 if horizon is None else 1.0 - 0.65 * _clamp(float(horizon) / 900.0, 0.0, 1.0)
    fields_component = 1.0 - 0.16 * len(missing)
    uncertainty_component = 1.0 - _clamp(uncertainty / 5.0, 0.0, 1.0)

    score = (
        0.23 * freshness_component
        + 0.12 * observation_component
        + 0.10 * timing_component
        + 0.12 * heading_component
        + 0.08 * speed_component
        + 0.10 * turn_component
        + 0.05 * trend_component
        + 0.10 * horizon_component
        + 0.05 * fields_component
        + 0.05 * uncertainty_component
    )
    # Keep continuity with the already-tested confidence engine while letting
    # the explicit evidence model cap overconfidence under degraded evidence.
    score = 0.75 * score + 0.25 * float(prediction.confidence_score)
    ground = _ground_evidence(samples)
    reasons: list[str] = []
    if age > freshness:
        reasons.append("speed-dependent freshness limit exceeded")
    if len(samples) < 3:
        reasons.append("short observation history")
    if irregular_timing or (interval_spread is not None and interval_spread > 5.0):
        reasons.append("irregular observation timing")
    if heading_spread is not None and heading_spread > 15.0:
        reasons.append("heading instability")
    if speed_spread is not None and speed_spread > 30.0:
        reasons.append("groundspeed instability")
    if abs(float(prediction.turn_rate_deg_s or 0.0)) >= 0.06 and not turn.stable:
        reasons.append("turn evidence is not stable")
    if uncertainty > 2.5:
        reasons.append("position uncertainty elevated")
    if horizon is not None and horizon > 420.0:
        reasons.append("long prediction horizon")
    if missing:
        reasons.append("missing " + ",".join(missing))
    if ground:
        reasons.append("multiple observations indicate ground movement")

    if age > freshness:
        score = min(score, 0.25)
    if ground:
        score = min(score, 0.20)
    # Preserve the v4.4 direct-presence recovery: its two fresh independent
    # in-radius observations are stronger than a short-history confidence hold.
    if (
        str(getattr(prediction, "reason", "")).startswith("two fresh independent ADS-B positions")
        and age <= freshness
        and not ground
    ):
        score = max(score, 0.90)
    score = _clamp(score, 0.0, 0.98)
    label = "High" if score >= 0.78 else "Medium" if score >= 0.58 else "Low" if score >= 0.36 else "Uncertain"
    return ConfidenceEvidence(
        score=score,
        label=label,
        observation_age_s=age,
        freshness_limit_s=freshness,
        position_uncertainty_km=uncertainty,
        observation_count=len(samples),
        observation_span_s=span,
        update_interval_median_s=interval_median,
        update_interval_spread_s=interval_spread,
        heading_stability_deg=heading_spread,
        speed_stability_kts=speed_spread,
        turn_stability=turn.reason,
        acceleration_kts_s=float(prediction.acceleration_kts_s or 0.0),
        distance_trend_available=prediction.distance_trend_km_s is not None,
        prediction_horizon_s=prediction.time_to_cpa_s,
        missing_fields=tuple(missing),
        ground_evidence=ground,
        reasons=tuple(reasons),
    )


def _record_diagnostics(prediction: t.TrajectoryPrediction, payload: dict[str, Any]) -> None:
    now = time.monotonic()
    _diagnostics[id(prediction)] = (now, payload)
    _diagnostics.move_to_end(id(prediction))
    cutoff = now - _DIAGNOSTIC_TTL_S
    for key, (seen, _) in list(_diagnostics.items()):
        if seen >= cutoff:
            break
        _diagnostics.pop(key, None)
    while len(_diagnostics) > _DIAGNOSTIC_MAX:
        _diagnostics.popitem(last=False)


def diagnostics_for(prediction: Any, *, consume: bool = False) -> dict[str, Any]:
    entry = _diagnostics.get(id(prediction))
    if not entry:
        return {}
    if time.monotonic() - entry[0] > _DIAGNOSTIC_TTL_S:
        _diagnostics.pop(id(prediction), None)
        return {}
    if consume:
        _diagnostics.pop(id(prediction), None)
    return dict(entry[1])


def predict_trajectory_v46(
    samples: Iterable[t.HistorySample],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    *,
    now: float | None = None,
    user_altitude_m: float = 0.0,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> t.TrajectoryPrediction:
    ordered = sorted(list(samples), key=lambda item: item.timestamp)
    effective_now = time.time() if now is None else float(now)
    prediction = _BASE_PREDICT(
        ordered,
        user_lat,
        user_lon,
        alert_radius_km,
        now=effective_now,
        user_altitude_m=user_altitude_m,
        max_horizon_s=max_horizon_s,
        step_s=step_s,
    )
    if not ordered:
        return prediction

    contiguous = t._contiguous_recent(ordered)  # type: ignore[attr-defined]
    evidence = _confidence_evidence(contiguous, prediction, now=effective_now, raw_samples=ordered)
    turn = turn_evidence(contiguous, now=effective_now)
    linear = _shadow_simulation(
        contiguous, user_lat, user_lon, alert_radius_km,
        now=effective_now, use_turn=False, evidence=turn, max_horizon_s=min(max_horizon_s, _SHADOW_MAX_HORIZON_S), step_s=step_s,
    )
    curved = _shadow_simulation(
        contiguous, user_lat, user_lon, alert_radius_km,
        now=effective_now, use_turn=True, evidence=turn, max_horizon_s=min(max_horizon_s, _SHADOW_MAX_HORIZON_S), step_s=step_s,
    )

    stale = bool(prediction.stale or evidence.observation_age_s > evidence.freshness_limit_s)
    state = prediction.state
    reason = prediction.reason
    enters = prediction.enters_alert_radius
    turning_away = prediction.turning_away
    already_passed = prediction.already_passed
    radius_entry = prediction.radius_entry_s

    if evidence.ground_evidence:
        stale = False
        state = "Prediction uncertain"
        enters = False
        turning_away = False
        already_passed = False
        radius_entry = None
        reason = "multiple recent observations indicate ground movement; aerial pass prediction withheld"
    elif stale:
        state = "Prediction uncertain"
        enters = False
        turning_away = False
        already_passed = False
        radius_entry = None
        reason = (
            f"position age {evidence.observation_age_s:.1f}s exceeds "
            f"{evidence.freshness_limit_s:.1f}s speed-dependent freshness limit"
        )

    updated = replace(
        prediction,
        state=state,
        confidence=evidence.label,
        confidence_score=evidence.score,
        enters_alert_radius=enters,
        already_passed=already_passed,
        turning_away=turning_away,
        stale=stale,
        radius_entry_s=radius_entry,
        reason=reason,
    )
    _record_diagnostics(updated, {
        "production_model": PRODUCTION_MODEL_VERSION,
        "confidence": asdict(evidence),
        "linear_shadow": asdict(linear),
        "turn_shadow": asdict(curved),
        "turn_evidence": asdict(turn),
        "shadow_only": True,
    })
    return updated


def reset_v46_state_for_tests() -> None:
    _diagnostics.clear()


def install_prediction_v46() -> None:
    global _INSTALLED, _BASE_PREDICT
    if _INSTALLED:
        return
    _BASE_PREDICT = t.predict_trajectory
    t.predict_trajectory = predict_trajectory_v46
    _INSTALLED = True
