"""Plane Alerts v4.2 deterministic terminal-arrival ensemble guard.

The existing trajectory predictor remains the fast live detector.  This module
controls promotion of that detector into a user-visible alert by comparing a
small set of plausible deterministic futures.  Route/airport/history evidence
may delay or veto a projected alert, but it may never create one and it loses
authority when live motion disproves the expected turn.

No AI model or paid API participates in trajectory, CPA, ETA, pass/no-pass,
turn, route, qualification, cancellation, or notification timing decisions.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any, Iterable

from app.intelligence import route_guard_v2 as v2
from app.intelligence import route_history as route_mod
from app.intelligence.trajectory import KNOTS_TO_KM_S, bearing_deg, haversine_km, project_point

logger = logging.getLogger(__name__)

# Centralized calibration.  These values are intentionally asymmetric:
# qualification requires materially stronger evidence than keeping an already
# qualified encounter alive.  They were chosen against the 2026-09-18
# production cancellation traces plus the Error Museum cases in tests.
QUALIFY_SCORE = 0.68
TERMINAL_QUALIFY_SCORE = 0.76
CANCEL_SCORE = 0.42
QUALIFY_CONFIRMATIONS = 2
TERMINAL_CONFIRMATIONS = 3
FRESH_CONFIRMATION_GAP_S = 22.0
EXPECTED_TURN_MIN_S = 20.0
EXPECTED_TURN_MAX_S = 90.0

_PATH_CACHE_MAX = 128
_PATH_CACHE_TTL_S = 30.0
_ENCOUNTER_MAX = 2048
_ENCOUNTER_TTL_S = 1800.0

_path_cache: "OrderedDict[tuple, tuple[float, list[MotionPath]]]" = OrderedDict()
_encounters: "OrderedDict[tuple, EncounterState]" = OrderedDict()
_INSTALLED = False


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _angle_delta(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


@dataclass(slots=True, frozen=True)
class MotionPoint:
    seconds: float
    latitude: float
    longitude: float


@dataclass(slots=True, frozen=True)
class MotionPath:
    name: str
    weight: float
    points: tuple[MotionPoint, ...]


@dataclass(slots=True, frozen=True)
class HypothesisResult:
    name: str
    weight: float
    cpa_km: float
    time_to_cpa_s: float
    enters_radius: bool


@dataclass(slots=True, frozen=True)
class ArrivalAssessment:
    terminal_state: str
    terminal_score: float
    pass_score: float
    median_cpa_km: float
    p10_cpa_km: float
    p90_cpa_km: float
    prediction_spread_km: float
    expected_turn_state: str
    expected_turn_direction: str
    expected_turn_eta_s: float | None
    airport_path_cpa_km: float | None
    historical_match_score: float
    historical_cluster: str
    live_pass_score: float = 0.0
    hypotheses: tuple[HypothesisResult, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class RouteGateResultV42(route_mod.RouteGateResult):
    terminal_arrival_state: str = "NOT_TERMINAL"
    terminal_arrival_score: float = 0.0
    ensemble_pass_score: float = 0.0
    live_pass_score: float = 0.0
    ensemble_median_cpa_km: float | None = None
    ensemble_p10_cpa_km: float | None = None
    ensemble_p90_cpa_km: float | None = None
    expected_turn_state: str = "NONE"
    expected_turn_direction: str = ""
    expected_turn_eta_s: float | None = None
    airport_path_cpa_km: float | None = None
    historical_match_score: float = 0.0
    qualification_state: str = "SHADOW"
    qualification_confirmations: int = 0


@dataclass(slots=True)
class EncounterState:
    last_seen_mono: float
    first_seen_mono: float
    positive_count: int = 0
    qualified: bool = False
    passed: bool = False
    passed_mono: float | None = None
    observed_min_km: float = math.inf
    previous_distance_km: float | None = None
    expected_turn_started: bool = False
    expected_turn_confirmed: bool = False
    expected_turn_deadline: float | None = None
    last_observation_at: float | None = None


def _prune(now_mono: float) -> None:
    for key in list(_path_cache):
        created, _ = _path_cache[key]
        if now_mono - created > _PATH_CACHE_TTL_S:
            _path_cache.pop(key, None)
    while len(_path_cache) > _PATH_CACHE_MAX:
        _path_cache.popitem(last=False)

    for key in list(_encounters):
        if now_mono - _encounters[key].last_seen_mono > _ENCOUNTER_TTL_S:
            _encounters.pop(key, None)
    while len(_encounters) > _ENCOUNTER_MAX:
        _encounters.popitem(last=False)


def _sample_lat_lon(value: Any) -> tuple[float, float] | None:
    try:
        if isinstance(value, dict):
            return float(value.get("latitude", value.get("lat"))), float(value.get("longitude", value.get("lon")))
        return float(value.latitude), float(value.longitude)
    except (AttributeError, TypeError, ValueError):
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError, IndexError):
            return None


def _destination_trend(samples: Iterable[Any], destination: route_mod.AirportInfo | None) -> float | None:
    if destination is None or destination.latitude is None or destination.longitude is None:
        return None
    values: list[tuple[float, float]] = []
    for item in list(samples)[-8:]:
        point = _sample_lat_lon(item)
        if point is None:
            continue
        timestamp = float(getattr(item, "timestamp", 0.0) or 0.0)
        values.append((timestamp, haversine_km(point[0], point[1], destination.latitude, destination.longitude)))
    if len(values) < 2:
        return None
    first_t, first_d = values[0]
    last_t, last_d = values[-1]
    dt = last_t - first_t
    return (last_d - first_d) / dt if dt > 1.0 else None


def _history_features(
    current_path: Iterable[Any],
    historical_paths: Iterable[Iterable[Any]],
    *,
    observer_lat: float,
    observer_lon: float,
    radius_km: float,
    aircraft_lat: float,
    aircraft_lon: float,
) -> tuple[float, str, float | None]:
    current = route_mod._clean_points(current_path)
    histories = [route_mod._clean_points(path) for path in historical_paths]
    histories = [path for path in histories if len(path) >= 3]
    if not histories or len(current) < 2:
        return 0.0, "none", None

    pairwise = route_mod._pairwise_history_similarity(histories)
    inconsistency = median(pairwise) if pairwise else 0.0
    if inconsistency > max(7.5, radius_km * 0.45):
        return 0.0, "dispersed", None

    similarities = [route_mod._current_to_history_similarity_km(current, path) for path in histories]
    similarities = [value for value in similarities if value is not None]
    if not similarities:
        return 0.0, "none", None
    similarity = median(similarities)
    scale = max(5.0, radius_km * 0.35)
    match_score = 1.0 - _clamp(similarity / (scale * 1.8), 0.0, 1.0)

    future_cpas: list[float] = []
    for path in histories:
        nearest = min(
            range(len(path)),
            key=lambda index: haversine_km(aircraft_lat, aircraft_lon, path[index].latitude, path[index].longitude),
        )
        future = path[nearest:]
        if future:
            future_cpas.append(min(haversine_km(p.latitude, p.longitude, observer_lat, observer_lon) for p in future))
    history_cpa = median(future_cpas) if future_cpas else None
    cluster = "matched" if match_score >= 0.65 else "weak-match" if match_score >= 0.35 else "diverged"
    return match_score, cluster, history_cpa


def _terminal_arrival_score(
    *,
    destination: route_mod.AirportInfo | None,
    route_plausible: bool,
    destination_distance_km: float | None,
    destination_trend_km_s: float | None,
    altitude_m: float | None,
    vertical_rate_mps: float | None,
    speed_kts: float | None,
    heading_deg: float | None,
    aircraft_lat: float,
    aircraft_lon: float,
    historical_match_score: float,
) -> tuple[float, str]:
    score = 0.0
    signals = 0
    if route_plausible and destination and destination.latitude is not None and destination.longitude is not None:
        score += 0.25
        signals += 1
        if destination_distance_km is not None and destination_distance_km <= 240.0:
            score += 0.15
            signals += 1
            if destination_distance_km <= 160.0:
                score += 0.05
        if destination_trend_km_s is not None and destination_trend_km_s < -0.015:
            score += 0.20
            signals += 1
        if vertical_rate_mps is not None and vertical_rate_mps <= -0.5:
            score += 0.20
            signals += 1
        elif altitude_m is not None and altitude_m <= 6500.0:
            score += 0.10
            signals += 1
        if altitude_m is not None and altitude_m <= 9000.0:
            score += 0.08
            signals += 1
        if speed_kts is not None and speed_kts <= 360.0:
            score += 0.04
        if heading_deg is not None:
            airport_bearing = bearing_deg(aircraft_lat, aircraft_lon, destination.latitude, destination.longitude)
            if abs(_angle_delta(airport_bearing, heading_deg)) <= 100.0:
                score += 0.08
                signals += 1

    # Missing destination never gets the same authority as a resolved route.
    # It can only produce the weaker ARRIVAL_LIKELY_HISTORY state when recent
    # live shape strongly matches bounded history plus descent/low altitude.
    if destination is None and historical_match_score >= 0.70:
        score += 0.28
        signals += 1
        if vertical_rate_mps is not None and vertical_rate_mps <= -0.5:
            score += 0.18
            signals += 1
        if altitude_m is not None and altitude_m <= 7000.0:
            score += 0.16
            signals += 1

    score = _clamp(score, 0.0, 1.0)
    if destination is not None and score >= 0.60 and signals >= 3:
        return score, "TERMINAL_ARRIVAL"
    if destination is None and score >= 0.50 and signals >= 3:
        return score, "ARRIVAL_LIKELY_HISTORY"
    return score, "NOT_TERMINAL"


def _simulate_path(
    *,
    name: str,
    weight: float,
    lat: float,
    lon: float,
    heading_deg: float,
    speed_kts: float,
    horizon_s: int,
    turn_rate_deg_s: float = 0.0,
    turn_decay_s: float | None = None,
    destination: route_mod.AirportInfo | None = None,
    convergence_rate_deg_s: float | None = None,
    convergence_delay_s: float = 0.0,
) -> MotionPath:
    step_s = 5
    points = [MotionPoint(0.0, lat, lon)]
    current_lat, current_lon, heading = lat, lon, heading_deg % 360.0
    for seconds in range(step_s, horizon_s + 1, step_s):
        rate = turn_rate_deg_s
        if turn_decay_s is not None:
            rate *= max(0.0, 1.0 - (seconds - step_s) / turn_decay_s)
        if destination and destination.latitude is not None and destination.longitude is not None and convergence_rate_deg_s is not None and seconds > convergence_delay_s:
            target = bearing_deg(current_lat, current_lon, destination.latitude, destination.longitude)
            delta = _angle_delta(target, heading)
            max_turn = convergence_rate_deg_s * step_s
            heading = (heading + _clamp(delta, -max_turn, max_turn)) % 360.0
        else:
            heading = (heading + rate * step_s) % 360.0
        current_lat, current_lon = project_point(current_lat, current_lon, speed_kts * KNOTS_TO_KM_S * step_s, heading)
        points.append(MotionPoint(float(seconds), current_lat, current_lon))
    return MotionPath(name, weight, tuple(points))


def _motion_paths(*, ac: Any, pred: Any, destination: route_mod.AirportInfo | None, terminal_state: str) -> list[MotionPath]:
    lat, lon = float(ac.latitude), float(ac.longitude)
    heading = float(getattr(ac, "heading", 0.0) or 0.0)
    speed = max(80.0, float(getattr(ac, "ground_speed", 0.0) or 0.0))
    turn_rate = float(getattr(pred, "turn_rate_deg_s", 0.0) or 0.0)
    dest_key = (round(float(destination.latitude), 3), round(float(destination.longitude), 3)) if destination and destination.latitude is not None and destination.longitude is not None else None
    cache_key = (
        str(getattr(ac, "icao24", "")).lower(),
        route_mod.normalize_flight_key(getattr(ac, "callsign", "")), round(lat, 4), round(lon, 4),
        round(heading, 1), round(speed, 0), round(turn_rate, 2), dest_key, terminal_state,
    )
    now_mono = time.monotonic()
    cached = _path_cache.get(cache_key)
    if cached and now_mono - cached[0] <= _PATH_CACHE_TTL_S:
        _path_cache.move_to_end(cache_key)
        return cached[1]

    horizon_s = 600
    terminal = terminal_state != "NOT_TERMINAL"
    paths = [
        _simulate_path(name="constant_heading", weight=0.45 if terminal else 1.0, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s),
        _simulate_path(name="observed_turn", weight=1.20 if abs(turn_rate) >= 0.08 else 0.55, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=_clamp(turn_rate, -1.8, 1.8), turn_decay_s=75.0),
        _simulate_path(name="curvature", weight=1.00 if abs(turn_rate) >= 0.08 else 0.40, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=_clamp(turn_rate, -1.2, 1.2), turn_decay_s=150.0),
        _simulate_path(name="shallow_left", weight=0.40, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=-0.35, turn_decay_s=120.0),
        _simulate_path(name="shallow_right", weight=0.40, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=0.35, turn_decay_s=120.0),
        _simulate_path(name="moderate_left", weight=0.28, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=-0.75, turn_decay_s=90.0),
        _simulate_path(name="moderate_right", weight=0.28, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, turn_rate_deg_s=0.75, turn_decay_s=90.0),
    ]
    if terminal and destination and destination.latitude is not None and destination.longitude is not None:
        paths.extend([
            _simulate_path(name="airport_convergence_now", weight=2.00, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, destination=destination, convergence_rate_deg_s=0.75),
            _simulate_path(name="airport_convergence_25s", weight=1.45, lat=lat, lon=lon, heading_deg=heading, speed_kts=speed, horizon_s=horizon_s, destination=destination, convergence_rate_deg_s=0.75, convergence_delay_s=25.0),
        ])
    _path_cache[cache_key] = (now_mono, paths)
    _path_cache.move_to_end(cache_key)
    _prune(now_mono)
    return paths


def _evaluate_motion_path(path: MotionPath, *, observer_lat: float, observer_lon: float, radius_km: float) -> HypothesisResult:
    seconds, cpa = min(
        ((point.seconds, haversine_km(point.latitude, point.longitude, observer_lat, observer_lon)) for point in path.points),
        key=lambda item: item[1],
    )
    return HypothesisResult(path.name, path.weight, cpa, seconds, cpa <= radius_km)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower, upper = int(math.floor(index)), int(math.ceil(index))
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _weighted_pass_score(hypotheses: Iterable[HypothesisResult]) -> float:
    values = list(hypotheses)
    total = sum(item.weight for item in values) or 1.0
    passing = sum(item.weight for item in values if item.enters_radius)
    return _clamp(passing / total, 0.0, 1.0)


def assess_candidate(
    *, ac: Any, pred: Any, destination: route_mod.AirportInfo | None,
    route_plausible: bool, current_samples: Iterable[Any],
    historical_paths: Iterable[Iterable[Any]], observer_lat: float,
    observer_lon: float, alert_radius_km: float,
    encounter: EncounterState | None = None, now_mono: float | None = None,
) -> ArrivalAssessment:
    now_mono = time.monotonic() if now_mono is None else now_mono
    aircraft_lat, aircraft_lon = float(ac.latitude), float(ac.longitude)
    destination_distance = None
    if destination and destination.latitude is not None and destination.longitude is not None:
        destination_distance = haversine_km(aircraft_lat, aircraft_lon, destination.latitude, destination.longitude)
    destination_trend = _destination_trend(current_samples, destination)
    history_match, history_cluster, history_cpa = _history_features(
        current_samples, historical_paths, observer_lat=observer_lat,
        observer_lon=observer_lon, radius_km=alert_radius_km,
        aircraft_lat=aircraft_lat, aircraft_lon=aircraft_lon,
    )
    terminal_score, terminal_state = _terminal_arrival_score(
        destination=destination, route_plausible=route_plausible,
        destination_distance_km=destination_distance,
        destination_trend_km_s=destination_trend,
        altitude_m=getattr(ac, "altitude", None),
        vertical_rate_mps=getattr(ac, "vertical_rate_mps", None),
        speed_kts=getattr(ac, "ground_speed", None),
        heading_deg=getattr(ac, "heading", None), aircraft_lat=aircraft_lat,
        aircraft_lon=aircraft_lon, historical_match_score=history_match,
    )

    hypotheses = [
        _evaluate_motion_path(path, observer_lat=observer_lat, observer_lon=observer_lon, radius_km=alert_radius_km)
        for path in _motion_paths(ac=ac, pred=pred, destination=destination, terminal_state=terminal_state)
    ]
    live_hypotheses = [item for item in hypotheses if not item.name.startswith("airport_convergence")]
    live_pass_score = _weighted_pass_score(live_hypotheses)

    if history_cpa is not None and history_match >= 0.35:
        history_weight = (1.5 if terminal_state != "NOT_TERMINAL" else 0.85) * history_match
        hypotheses.append(HypothesisResult(
            "historical_continuation", history_weight, history_cpa,
            float(getattr(pred, "time_to_cpa_s", 0.0) or 0.0), history_cpa <= alert_radius_km,
        ))

    pass_score = _weighted_pass_score(hypotheses)
    cpas = [item.cpa_km for item in hypotheses]
    p10, p90 = _percentile(cpas, 0.10), _percentile(cpas, 0.90)
    med = median(cpas) if cpas else math.inf
    airport_cpas = [item.cpa_km for item in hypotheses if item.name.startswith("airport_convergence")]
    airport_cpa = min(airport_cpas) if airport_cpas else None

    expected_state, expected_direction, expected_eta = "NONE", "", None
    heading = getattr(ac, "heading", None)
    if terminal_state != "NOT_TERMINAL" and destination and heading is not None:
        target = bearing_deg(aircraft_lat, aircraft_lon, destination.latitude, destination.longitude)
        required = _angle_delta(target, float(heading))
        turn_rate = float(getattr(pred, "turn_rate_deg_s", 0.0) or 0.0)
        deadline = None
        if encounter is not None:
            if encounter.expected_turn_deadline is None and abs(required) >= 12.0:
                encounter.expected_turn_deadline = now_mono + min(
                    EXPECTED_TURN_MAX_S,
                    max(EXPECTED_TURN_MIN_S, float(getattr(pred, "time_to_cpa_s", EXPECTED_TURN_MAX_S) or EXPECTED_TURN_MAX_S) * 0.35),
                )
            deadline = encounter.expected_turn_deadline

        if encounter is not None and encounter.expected_turn_confirmed:
            expected_state = "TURN_CONFIRMED"
        elif abs(required) < 12.0:
            if encounter is not None and encounter.expected_turn_started:
                encounter.expected_turn_confirmed = True
                expected_state = "TURN_CONFIRMED"
        else:
            expected_direction = "RIGHT" if required > 0 else "LEFT"
            expected_eta = _clamp(abs(required) / 0.75, EXPECTED_TURN_MIN_S, EXPECTED_TURN_MAX_S)
            aligned = (required > 0 and turn_rate > 0.12) or (required < 0 and turn_rate < -0.12)
            if aligned:
                if encounter is not None:
                    encounter.expected_turn_started = True
                expected_state = "TURN_STARTED"
            elif deadline is not None and now_mono >= deadline:
                expected_state = "TURN_DID_NOT_OCCUR"
            else:
                expected_state = "EXPECTED_TURN_PENDING"

    return ArrivalAssessment(
        terminal_state=terminal_state, terminal_score=terminal_score,
        pass_score=pass_score, median_cpa_km=med, p10_cpa_km=p10,
        p90_cpa_km=p90, prediction_spread_km=max(0.0, p90 - p10),
        expected_turn_state=expected_state, expected_turn_direction=expected_direction,
        expected_turn_eta_s=expected_eta, airport_path_cpa_km=airport_cpa,
        historical_match_score=history_match, historical_cluster=history_cluster,
        live_pass_score=live_pass_score, hypotheses=tuple(hypotheses),
    )


def _encounter_key(ac: Any, user_lat: float, user_lon: float, radius_km: float) -> tuple:
    return (
        str(getattr(ac, "icao24", "")).lower(),
        route_mod.normalize_flight_key(getattr(ac, "callsign", "")), round(float(user_lat), 4),
        round(float(user_lon), 4), round(float(radius_km), 1),
    )


def _neutralize_unsafe_history_veto(result: route_mod.RouteGateResult) -> route_mod.RouteGateResult:
    reason = str(result.reason or "").lower()
    if result.suppress_alert and ("today's route diverges" in reason or "last three flight-number routes are inconsistent" in reason):
        return replace(
            result, suppress_alert=False, expected_turn_pending=False,
            reason="historical routes are inconsistent/diverged; live trajectory regains authority",
        )
    return result


def _apply_qualification(
    *, base: route_mod.RouteGateResult, assessment: ArrivalAssessment,
    encounter: EncounterState, pred: Any, current_distance_km: float,
    radius_km: float, active: bool, now_mono: float, observed_at: float | None = None,
) -> tuple[bool, str, int]:
    observation = now_mono if observed_at is None else observed_at
    fresh_observation = encounter.last_observation_at is None or observation > encounter.last_observation_at + 0.001
    previous_observation = encounter.last_observation_at
    if fresh_observation:
        encounter.last_observation_at = observation
    if previous_observation is not None and observation - previous_observation > FRESH_CONFIRMATION_GAP_S:
        encounter.positive_count = 0
    previous_seen = encounter.last_seen_mono
    if not encounter.qualified and now_mono - previous_seen > FRESH_CONFIRMATION_GAP_S:
        encounter.positive_count = 0
    encounter.last_seen_mono = now_mono
    encounter.observed_min_km = min(encounter.observed_min_km, current_distance_km)

    moving_away = encounter.previous_distance_km is not None and current_distance_km > encounter.previous_distance_km + 0.15
    if bool(getattr(pred, "already_passed", False)) or str(getattr(pred, "state", "")) == "Passed":
        if moving_away or current_distance_km > encounter.observed_min_km + 0.3:
            encounter.passed, encounter.passed_mono = True, now_mono

    if encounter.passed:
        separated = current_distance_km >= max(radius_km * 2.2, radius_km + 12.0)
        inbound = float(getattr(pred, "distance_trend_km_s", 0.0) or 0.0) < -0.004
        old_enough = encounter.passed_mono is not None and now_mono - encounter.passed_mono >= 180.0
        if separated and inbound and old_enough:
            encounter.passed = False
            encounter.qualified = False
            encounter.positive_count = 0
            encounter.first_seen_mono = now_mono
            encounter.observed_min_km = current_distance_km
            encounter.expected_turn_started = False
            encounter.expected_turn_confirmed = False
            encounter.expected_turn_deadline = None
        else:
            encounter.previous_distance_km = current_distance_km
            return True, "PASSED_LOCKED", encounter.positive_count
    encounter.previous_distance_km = current_distance_km

    if bool(getattr(pred, "stale", False)):
        encounter.positive_count = 0
        return (not active), "SHADOW_DATA_UNCERTAIN", encounter.positive_count

    terminal = assessment.terminal_state != "NOT_TERMINAL"
    threshold = TERMINAL_QUALIFY_SCORE if terminal else QUALIFY_SCORE
    required = TERMINAL_CONFIRMATIONS if terminal else QUALIFY_CONFIRMATIONS
    cpa_s = float(getattr(pred, "time_to_cpa_s", 9999.0) or 9999.0)
    if cpa_s <= 90.0 or current_distance_km <= radius_km * 1.20:
        required = min(required, 2)

    expected_turn_holds = (
        terminal
        and assessment.expected_turn_state in {"EXPECTED_TURN_PENDING", "TURN_STARTED", "TURN_CONFIRMED"}
        and assessment.airport_path_cpa_km is not None
        and assessment.airport_path_cpa_km > radius_km + max(2.0, radius_km * 0.15)
    )

    effective_score = assessment.pass_score
    if assessment.expected_turn_state == "TURN_DID_NOT_OCCUR":
        # Critical fail-safe: once reality misses the bounded expected-turn
        # window, airport/history hypotheses stop dominating qualification.
        # The score falls back to live motion only, so a genuine pass can still
        # alert while there is useful lead time.
        expected_turn_holds = False
        effective_score = assessment.live_pass_score
        threshold = QUALIFY_SCORE
        required = min(required, 2)

    strong_live_pass = effective_score >= threshold
    if active:
        if expected_turn_holds:
            return True, assessment.expected_turn_state, encounter.positive_count
        if effective_score < CANCEL_SCORE:
            return True, "ACTIVE_BELOW_CANCEL_HYSTERESIS", encounter.positive_count
        encounter.qualified = True
        return False, "QUALIFIED_PASS", encounter.positive_count

    if base.suppress_alert and not (assessment.expected_turn_state == "TURN_DID_NOT_OCCUR" and strong_live_pass):
        encounter.positive_count = 0
        return True, "SHADOW_ROUTE_EVIDENCE", encounter.positive_count
    if expected_turn_holds:
        encounter.positive_count = 0
        return True, assessment.expected_turn_state, encounter.positive_count

    if fresh_observation:
        encounter.positive_count = encounter.positive_count + 1 if strong_live_pass else 0
    if encounter.positive_count < required:
        return True, "TRAJECTORY_CONFIRMING", encounter.positive_count
    encounter.qualified = True
    return False, "QUALIFIED_PASS", encounter.positive_count


async def evaluate_route_v42(
    self: route_mod.RouteHistoryService, ac: Any, pred: Any, *, user_lat: float,
    user_lon: float, alert_radius_km: float, current_samples: Iterable[Any],
) -> route_mod.RouteGateResult:
    current_samples = list(current_samples)
    base = await v2.evaluate_route_nonblocking(
        self, ac, pred, user_lat=user_lat, user_lon=user_lon,
        alert_radius_km=alert_radius_km, current_samples=current_samples,
    )
    base = _neutralize_unsafe_history_veto(base)
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    history = await v2._historical_paths_cached(self, key) if key else []
    route, _ = v2._cached_route(self, key) if key else (None, False)

    now_mono = time.monotonic()
    encounter_key = _encounter_key(ac, user_lat, user_lon, alert_radius_km)
    encounter = _encounters.get(encounter_key)
    if encounter is None:
        encounter = EncounterState(last_seen_mono=now_mono, first_seen_mono=now_mono)
        _encounters[encounter_key] = encounter
    else:
        _encounters.move_to_end(encounter_key)

    assessment = assess_candidate(
        ac=ac, pred=pred, destination=route.destination if route else None,
        route_plausible=bool(route and route.plausible), current_samples=current_samples,
        historical_paths=history, observer_lat=user_lat, observer_lon=user_lon,
        alert_radius_km=alert_radius_km, encounter=encounter, now_mono=now_mono,
    )
    suppress, qualification_state, confirmations = _apply_qualification(
        base=base, assessment=assessment, encounter=encounter, pred=pred,
        current_distance_km=float(getattr(pred, "current_distance_km", math.inf)),
        radius_km=alert_radius_km, active=encounter.qualified, now_mono=now_mono,
        observed_at=max((float(item.timestamp) for item in current_samples if hasattr(item, "timestamp")), default=None),
    )

    directly_inside = (
        not bool(getattr(pred, "stale", False))
        and float(getattr(pred, "current_distance_km", math.inf)) <= alert_radius_km
    )
    if directly_inside:
        suppress = False
        qualification_state = "QUALIFIED_OBSERVED_INSIDE"
        encounter.qualified = True

    result = RouteGateResultV42(
        suppress_alert=suppress, callsign=base.callsign,
        reason=(
            base.reason if suppress and qualification_state == "SHADOW_ROUTE_EVIDENCE"
            else f"v4.2 {qualification_state}: ensemble={assessment.pass_score:.3f} live={assessment.live_pass_score:.3f}"
        ),
        history_days=base.history_days, similar_days=base.similar_days,
        similarity_km=base.similarity_km,
        destination_code=base.destination_code or (route.destination.code if route and route.destination else ""),
        destination_distance_km=base.destination_distance_km,
        expected_turn_pending=assessment.expected_turn_state in {"EXPECTED_TURN_PENDING", "TURN_STARTED"},
        route_plausible=base.route_plausible or bool(route and route.plausible),
        terminal_arrival_state=assessment.terminal_state,
        terminal_arrival_score=assessment.terminal_score,
        ensemble_pass_score=assessment.pass_score, live_pass_score=assessment.live_pass_score,
        ensemble_median_cpa_km=assessment.median_cpa_km,
        ensemble_p10_cpa_km=assessment.p10_cpa_km,
        ensemble_p90_cpa_km=assessment.p90_cpa_km,
        expected_turn_state=assessment.expected_turn_state,
        expected_turn_direction=assessment.expected_turn_direction,
        expected_turn_eta_s=assessment.expected_turn_eta_s,
        airport_path_cpa_km=assessment.airport_path_cpa_km,
        historical_match_score=assessment.historical_match_score,
        qualification_state=qualification_state,
        qualification_confirmations=confirmations,
    )
    logger.debug("v42_decision %s", json.dumps({
        "aircraft": getattr(ac, "icao24", ""), "flight": getattr(ac, "callsign", ""),
        "destination": result.destination_code,
        "terminal_arrival_state": result.terminal_arrival_state,
        "current_distance": round(float(getattr(pred, "current_distance_km", math.inf)), 3),
        "straight_line_cpa": round(float(getattr(pred, "projected_closest_km", math.inf)), 3),
        "ensemble_median_cpa": round(float(result.ensemble_median_cpa_km or math.inf), 3),
        "ensemble_p10_cpa": round(float(result.ensemble_p10_cpa_km or math.inf), 3),
        "ensemble_p90_cpa": round(float(result.ensemble_p90_cpa_km or math.inf), 3),
        "pass_score": round(result.ensemble_pass_score, 4),
        "live_pass_score": round(result.live_pass_score, 4),
        "prediction_confidence": getattr(pred, "confidence", ""),
        "historical_cluster": assessment.historical_cluster,
        "historical_match_score": round(result.historical_match_score, 4),
        "expected_turn": result.expected_turn_state,
        "expected_turn_direction": result.expected_turn_direction,
        "expected_turn_eta": result.expected_turn_eta_s,
        "airport_path_cpa": result.airport_path_cpa_km,
        "qualification_state": result.qualification_state,
        "qualification_confirmations": result.qualification_confirmations,
        "reason_suppressed": result.reason if result.suppress_alert else "",
    }, sort_keys=True, separators=(",", ":")))
    _prune(now_mono)
    return result


def reset_v42_state_for_tests() -> None:
    _path_cache.clear()
    _encounters.clear()


def install_route_guard_v42() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    route_mod.RouteHistoryService.evaluate = evaluate_route_v42
    _INSTALLED = True
    logger.info(
        "Plane Alerts v4.2 terminal-arrival ensemble enabled: qualify=%.2f terminal=%.2f cancel=%.2f confirmations=%d/%d",
        QUALIFY_SCORE, TERMINAL_QUALIFY_SCORE, CANCEL_SCORE,
        QUALIFY_CONFIRMATIONS, TERMINAL_CONFIRMATIONS,
    )
