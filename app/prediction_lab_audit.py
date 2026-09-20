"""Bounded, deterministic Prediction Lab telemetry.

This module records what Plane Alerts predicted before the outcome was known.
It never calls AI and never changes alert decisions. The AGY worker consumes a
sanitized copy of this data later to compare expectations with reality.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from types import SimpleNamespace

from app.database import get_db
from app.version import PREDICTION_VERSION
from app.worker.optional_work import OptionalCache

audit_work = OptionalCache(max_entries=4096, max_pending=64, concurrency=2, timeout=3.0)

logger = logging.getLogger(__name__)

# One snapshot per user/aircraft/minute is enough for calibration while keeping
# Mongo writes bounded even when "all aircraft" monitoring is enabled.
_SNAPSHOT_INTERVAL_S = 60.0
_MAX_THROTTLE_KEYS = 4096
_last_snapshot: dict[tuple[int, str], float] = {}


def _prune_throttle(now: float) -> None:
    if len(_last_snapshot) <= _MAX_THROTTLE_KEYS:
        return
    cutoff = now - 7200.0
    for key, seen in list(_last_snapshot.items()):
        if seen < cutoff:
            _last_snapshot.pop(key, None)
    if len(_last_snapshot) > _MAX_THROTTLE_KEYS:
        oldest = sorted(_last_snapshot.items(), key=lambda item: item[1])[: len(_last_snapshot) - _MAX_THROTTLE_KEYS]
        for key, _ in oldest:
            _last_snapshot.pop(key, None)


def _horizon_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "passed_or_invalid"
    if seconds <= 600:
        return "0-10m"
    if seconds <= 1800:
        return "10-30m"
    if seconds <= 3600:
        return "30-60m"
    return ">60m"


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _snapshot_diagnostics(aircraft: Any, prediction: Any, diagnostics: dict | None) -> dict:
    """Attach shadow/provenance evidence without retaining user location."""
    merged = dict(diagnostics or {})
    try:
        from app.intelligence.prediction_v46 import diagnostics_for

        v46 = diagnostics_for(prediction)
        if v46:
            merged["v46"] = v46
    except Exception:
        logger.debug("v46_prediction_diagnostics_unavailable", exc_info=True)

    try:
        from app.intelligence.airport_terminal_v47 import diagnostics_for as terminal_diagnostics_for

        v47 = terminal_diagnostics_for(prediction)
        if v47:
            merged["v47"] = v47
    except Exception:
        logger.debug("v47_terminal_diagnostics_unavailable", exc_info=True)

    candidates = getattr(aircraft, "source_candidates", ()) or ()
    if isinstance(candidates, dict):
        candidates = list(candidates)
    elif not isinstance(candidates, (list, tuple, set)):
        candidates = [str(candidates)] if candidates else []

    provenance = getattr(aircraft, "field_provenance", None)
    if not isinstance(provenance, dict):
        provenance = {}

    merge_notes = getattr(aircraft, "merge_notes", ()) or ()
    if not isinstance(merge_notes, (list, tuple, set)):
        merge_notes = [str(merge_notes)] if merge_notes else []

    merged["observation"] = {
        "source": str(getattr(aircraft, "source", "") or ""),
        "source_candidates": [str(item) for item in candidates][:8],
        "source_freshness": str(getattr(aircraft, "source_freshness", "") or ""),
        "data_quality": str(getattr(aircraft, "data_quality", "") or ""),
        "field_provenance": {str(key): str(value) for key, value in list(provenance.items())[:16]},
        "merge_notes": [str(item)[:160] for item in list(merge_notes)[:8]],
    }
    merged["proximity_3d"] = {
        "available": bool(getattr(prediction, "three_d_available", False)),
        "observer_altitude_known": bool(getattr(prediction, "observer_altitude_known", False)),
        "current_vertical_separation_m": _safe_float(getattr(prediction, "current_vertical_separation_m", None)),
        "projected_closest_3d_km": _safe_float(getattr(prediction, "projected_closest_3d_km", None)),
        "projected_closest_3d_lower_bound_km": _safe_float(getattr(prediction, "projected_closest_3d_lower_bound_km", None)),
        "time_to_3d_cpa_s": _safe_float(getattr(prediction, "time_to_3d_cpa_s", None)),
        "horizontal_at_3d_cpa_km": _safe_float(getattr(prediction, "horizontal_at_3d_cpa_km", None)),
        "vertical_at_3d_cpa_m": _safe_float(getattr(prediction, "vertical_at_3d_cpa_m", None)),
        "altitude_confidence": str(getattr(prediction, "altitude_confidence", "Unavailable") or "Unavailable"),
        "altitude_uncertainty_m": _safe_float(getattr(prediction, "altitude_uncertainty_m", None)),
        "altitude_relevance_applied": bool(getattr(prediction, "altitude_relevance_applied", False)),
        "altitude_relevance_reason": str(getattr(prediction, "altitude_relevance_reason", "") or "")[:500],
    }
    return merged


async def record_prediction_snapshot(
    *,
    user_id: int,
    aircraft: Any,
    prediction: Any,
    alert_radius_km: float,
    qualifies: bool,
    route_suppressed: bool,
    route_reason: str = "",
    diagnostics: dict | None = None,
) -> None:
    """Persist a pre-outcome prediction, rate-limited and TTL bounded."""
    now_mono = time.monotonic()
    icao = str(getattr(aircraft, "icao24", "") or "").lower().strip()
    if not icao:
        return
    key = (int(user_id), icao)
    previous = _last_snapshot.get(key, 0.0)
    if now_mono - previous < _SNAPSHOT_INTERVAL_S:
        return
    _last_snapshot[key] = now_mono
    _prune_throttle(now_mono)

    now = datetime.now(timezone.utc)
    t_cpa = _safe_float(getattr(prediction, "time_to_cpa_s", None))

    doc = {
        "kind": "prediction",
        "prediction_version": PREDICTION_VERSION,
        "diagnostics": diagnostics or {},
        "captured_at": now,
        "expires_at": now + timedelta(days=4),
        "user_id": int(user_id),
        "aircraft_icao24": icao,
        "callsign": str(getattr(aircraft, "callsign", "") or "").strip(),
        "aircraft_type": str(getattr(aircraft, "aircraft_type", "") or getattr(aircraft, "display_type", "") or ""),
        "current_distance_km": float(getattr(prediction, "current_distance_km", 0.0) or 0.0),
        "current_slant_km": _safe_float(getattr(prediction, "current_slant_km", None)),
        "projected_closest_km": float(getattr(prediction, "projected_closest_km", 0.0) or 0.0),
        "projected_closest_3d_km": _safe_float(getattr(prediction, "projected_closest_3d_km", None)),
        "projected_closest_3d_lower_bound_km": _safe_float(getattr(prediction, "projected_closest_3d_lower_bound_km", None)),
        "time_to_cpa_s": t_cpa,
        "time_to_3d_cpa_s": _safe_float(getattr(prediction, "time_to_3d_cpa_s", None)),
        "horizon_bucket": _horizon_bucket(t_cpa),
        "state": str(getattr(prediction, "state", "") or ""),
        "decision_reason": str(getattr(prediction, "reason", "") or "")[:500],
        "confidence": str(getattr(prediction, "confidence", "") or ""),
        "confidence_score": float(getattr(prediction, "confidence_score", 0.0) or 0.0),
        "enters_alert_radius": bool(getattr(prediction, "enters_alert_radius", False)),
        "altitude_relevance_applied": bool(getattr(prediction, "altitude_relevance_applied", False)),
        "altitude_confidence": str(getattr(prediction, "altitude_confidence", "Unavailable") or "Unavailable"),
        "alert_radius_km": float(alert_radius_km),
        "qualifies": bool(qualifies),
        "route_suppressed": bool(route_suppressed),
        "route_reason": str(route_reason or "")[:500],
        # Important honesty marker: current live acquisition is regional, so a
        # lack of 30-60m examples must never be interpreted as forecast accuracy.
        "coverage_mode": "regional_adsb_current_predictor",
    }
    try:
        await get_db()["prediction_lab_audit"].insert_one(doc)
    except Exception:
        logger.exception("prediction_lab_snapshot_failed user=%s icao=%s", user_id, icao)


async def record_prediction_outcome(
    *,
    user_id: int,
    aircraft: Any,
    outcome: str,
    observed_closest_km: float,
    final_prediction: Any,
    previous_projected_closest_km: float | None = None,
    route_suppressed: bool = False,
    route_reason: str = "",
) -> None:
    """Persist ground truth/lifecycle outcome for later replay and calibration."""
    now = datetime.now(timezone.utc)
    icao = str(getattr(aircraft, "icao24", "") or "").lower().strip()
    previous_cpa = _safe_float(previous_projected_closest_km)
    doc = {
        "kind": "outcome",
        "prediction_version": PREDICTION_VERSION,
        "outcome_basis": "lifecycle_transition_only",
        "scoreable": False,
        "captured_at": now,
        "expires_at": now + timedelta(days=8),
        "user_id": int(user_id),
        "aircraft_icao24": icao,
        "callsign": str(getattr(aircraft, "callsign", "") or "").strip(),
        "aircraft_type": str(getattr(aircraft, "aircraft_type", "") or getattr(aircraft, "display_type", "") or ""),
        "outcome": str(outcome),
        "observed_closest_km": float(observed_closest_km),
        "previous_projected_closest_km": previous_cpa,
        "final_projected_closest_km": float(getattr(final_prediction, "projected_closest_km", 0.0) or 0.0),
        "final_projected_closest_3d_km": _safe_float(getattr(final_prediction, "projected_closest_3d_km", None)),
        "final_projected_closest_3d_lower_bound_km": _safe_float(getattr(final_prediction, "projected_closest_3d_lower_bound_km", None)),
        "final_time_to_cpa_s": getattr(final_prediction, "time_to_cpa_s", None),
        "final_time_to_3d_cpa_s": getattr(final_prediction, "time_to_3d_cpa_s", None),
        "final_state": str(getattr(final_prediction, "state", "") or ""),
        "final_confidence": str(getattr(final_prediction, "confidence", "") or ""),
        "final_altitude_relevance_applied": bool(getattr(final_prediction, "altitude_relevance_applied", False)),
        "route_suppressed": bool(route_suppressed),
        "route_reason": str(route_reason or "")[:500],
        "coverage_mode": "regional_adsb_current_predictor",
    }
    try:
        await get_db()["prediction_lab_audit"].insert_one(doc)
    except Exception:
        logger.exception("prediction_lab_outcome_failed user=%s icao=%s", user_id, icao)


def enqueue_snapshot(*, user_id, aircraft, prediction, alert_radius_km, qualifies, route_suppressed, route_reason="", diagnostics=None):
    # Capture diagnostics before replacing the live prediction object with a
    # scalar-only copy. This keeps shadow evidence bounded and joinable to the
    # existing Prediction Lab record without retaining projected paths.
    merged_diagnostics = _snapshot_diagnostics(aircraft, prediction, diagnostics)
    pred = SimpleNamespace(**{name: getattr(prediction, name, None) for name in (
        "time_to_cpa_s", "time_to_3d_cpa_s", "current_distance_km", "current_slant_km",
        "projected_closest_km", "projected_closest_3d_km", "projected_closest_3d_lower_bound_km",
        "state", "reason", "confidence", "confidence_score", "enters_alert_radius",
        "altitude_relevance_applied", "altitude_confidence")})
    ac = SimpleNamespace(icao24=aircraft.icao24, callsign=aircraft.callsign, aircraft_type=aircraft.aircraft_type)
    audit_work.get(("prediction", user_id, ac.icao24), lambda: record_prediction_snapshot(
        user_id=user_id, aircraft=ac, prediction=pred, alert_radius_km=alert_radius_km,
        qualifies=qualifies, route_suppressed=route_suppressed, route_reason=route_reason,
        diagnostics=merged_diagnostics), ttl=60)


def enqueue_outcome(**kwargs):
    aircraft = kwargs["aircraft"]
    audit_work.get(("outcome", kwargs["user_id"], aircraft.icao24, kwargs["outcome"]),
                   lambda: record_prediction_outcome(**kwargs), ttl=30)
