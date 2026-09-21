"""Emergency direct-presence qualifier for real close passes.

The normal trajectory confidence model remains authoritative for projected
alerts. This guard only upgrades an otherwise-uncertain prediction when two
fresh, independent ADS-B positions prove that the aircraft is physically
inside the configured radius and still approaching (or CPA is imminent).

This is intentionally narrow: stale samples, a single sample, duplicated
positions, history-only evidence, and missing coverage can never trigger it.
"""
from __future__ import annotations

from dataclasses import replace
import time
from typing import Iterable

from app.intelligence import trajectory as t

_MAX_SAMPLE_AGE_S = 20.0
_MIN_SAMPLE_GAP_S = 1.0
_MAX_SAMPLE_GAP_S = 22.0
_MIN_INDEPENDENT_MOVE_KM = 0.03
_MIN_APPROACH_DELTA_KM = 0.04
_INSTALLED = False
_BASE_PREDICT = t.predict_trajectory


def _sample_age(sample: t.HistorySample, now: float) -> float:
    return max(float(sample.position_age_s or 0.0), max(0.0, float(now) - float(sample.timestamp)))


def _confirmed_fresh_direct_presence(
    samples: Iterable[t.HistorySample],
    *,
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    now: float,
    prediction: t.TrajectoryPrediction,
) -> bool:
    ordered = sorted(list(samples), key=lambda sample: sample.timestamp)
    if len(ordered) < 2:
        return False

    latest = ordered[-1]
    if _sample_age(latest, now) > _MAX_SAMPLE_AGE_S:
        return False
    latest_distance = t.haversine_km(latest.latitude, latest.longitude, user_lat, user_lon)
    if latest_distance > float(alert_radius_km):
        return False

    previous = None
    for candidate in reversed(ordered[:-1]):
        gap = float(latest.timestamp) - float(candidate.timestamp)
        if gap > _MAX_SAMPLE_GAP_S:
            break
        if gap < _MIN_SAMPLE_GAP_S:
            continue
        if _sample_age(candidate, now) > _MAX_SAMPLE_AGE_S:
            continue
        movement = t.haversine_km(
            candidate.latitude,
            candidate.longitude,
            latest.latitude,
            latest.longitude,
        )
        if movement < _MIN_INDEPENDENT_MOVE_KM:
            continue
        previous = candidate
        break

    if previous is None:
        return False

    previous_distance = t.haversine_km(
        previous.latitude,
        previous.longitude,
        user_lat,
        user_lon,
    )
    decreasing = latest_distance <= previous_distance - _MIN_APPROACH_DELTA_KM
    imminent_cpa = (
        prediction.time_to_cpa_s is not None
        and 0.0 <= float(prediction.time_to_cpa_s) <= 45.0
        and float(prediction.projected_closest_km) <= float(alert_radius_km)
    )
    return decreasing or imminent_cpa


def predict_trajectory_v44(
    samples: Iterable[t.HistorySample],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    *,
    now: float | None = None,
    user_altitude_m: float | None = None,
    altitude_relevance: bool = True,
    max_horizon_s: int = 900,
    step_s: int = 3,
) -> t.TrajectoryPrediction:
    ordered = sorted(list(samples), key=lambda sample: sample.timestamp)
    effective_now = time.time() if now is None else float(now)
    prediction = _BASE_PREDICT(
        ordered,
        user_lat,
        user_lon,
        alert_radius_km,
        now=effective_now,
        user_altitude_m=user_altitude_m,
        altitude_relevance=altitude_relevance,
        max_horizon_s=max_horizon_s,
        step_s=step_s,
    )

    if not _confirmed_fresh_direct_presence(
        ordered,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=alert_radius_km,
        now=effective_now,
        prediction=prediction,
    ):
        return prediction

    # Physical evidence is stronger than a confidence hold, but only inside the
    # user's configured radius and only after two independent fresh positions.
    # Giving this evidence a high deterministic confidence lets the existing
    # route/qualification layer run, where direct in-radius presence already
    # overrides historical/model vetoes.
    return replace(
        prediction,
        state="Passing nearby",
        confidence="High",
        confidence_score=max(float(prediction.confidence_score), 0.90),
        enters_alert_radius=True,
        already_passed=False,
        turning_away=False,
        stale=False,
        radius_entry_s=0.0,
        reason="two fresh independent ADS-B positions confirm physical presence inside the configured alert radius",
    )


def install_direct_presence_guard_v44() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    t.predict_trajectory = predict_trajectory_v44
    _INSTALLED = True
