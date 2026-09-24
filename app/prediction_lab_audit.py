"""Bounded deterministic Prediction Lab telemetry for Plane Alerts v5.5.

v5.5 makes the persistent volume file spool authoritative for audit/evaluation
telemetry. Legacy Mongo writes continue only until the historical migration is
verified; they are never required for live alert correctness.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.database import get_db
from app.prediction_lab_files_v55 import append_evidence, migration_verified, observer_ref
from app.shadow_evaluation_v52 import evaluate_snapshot_outcome, feature_flags
from app.version import PREDICTION_VERSION, VERSION
from app.worker.optional_work import OptionalCache

audit_work = OptionalCache(max_entries=4096, max_pending=64, concurrency=2, timeout=3.0)
logger = logging.getLogger(__name__)

_SNAPSHOT_INTERVAL_S = 60.0
_MAX_THROTTLE_KEYS = 4096
_last_snapshot: dict[tuple[int, str, str], float] = {}
_MAX_CLOSEST_KEYS = 4096
_CLOSEST_RESET_S = 1800.0
_closest_observation: dict[tuple[int, str], tuple[float, datetime, float]] = {}
_recent_snapshots: dict[tuple[int, str], deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=24))


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


def _case_id(user_id: int, icao: str, when: datetime) -> str:
    material = f"{observer_ref(user_id)}|{icao}|{when.date().isoformat()}"
    return "case-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _track_closest_observation(user_id: int, icao: str, current_distance_km: Any, diagnostics: dict | None) -> None:
    distance = _safe_float(current_distance_km)
    observed_epoch = _safe_float((diagnostics or {}).get("last_observation_at"))
    if distance is None or distance < 0 or observed_epoch is None or observed_epoch <= 0:
        return
    try:
        observed_at = datetime.fromtimestamp(observed_epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return
    now_mono = time.monotonic()
    key = (int(user_id), str(icao).lower().strip())
    previous = _closest_observation.get(key)
    if previous is None or now_mono - previous[2] > _CLOSEST_RESET_S or distance <= previous[0]:
        _closest_observation[key] = (distance, observed_at, now_mono)
    else:
        _closest_observation[key] = (previous[0], previous[1], now_mono)
    if len(_closest_observation) > _MAX_CLOSEST_KEYS:
        stale_cutoff = now_mono - _CLOSEST_RESET_S
        for old_key, (_, _, seen) in list(_closest_observation.items()):
            if seen < stale_cutoff:
                _closest_observation.pop(old_key, None)
        if len(_closest_observation) > _MAX_CLOSEST_KEYS:
            oldest = sorted(_closest_observation.items(), key=lambda item: item[1][2])[: len(_closest_observation) - _MAX_CLOSEST_KEYS]
            for old_key, _ in oldest:
                _closest_observation.pop(old_key, None)


def _snapshot_diagnostics(aircraft: Any, prediction: Any, diagnostics: dict | None) -> dict:
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
        "position_age_s": _safe_float(getattr(aircraft, "position_age_s", None)),
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


async def _legacy_insert(collection: str, doc: dict[str, Any]) -> Any | None:
    if migration_verified():
        return None
    try:
        return await get_db()[collection].insert_one(doc)
    except Exception:
        logger.debug("prediction_lab_legacy_write_failed collection=%s", collection, exc_info=True)
        return None


async def record_prediction_snapshot(*, user_id: int, aircraft: Any, prediction: Any, alert_radius_km: float,
                                     qualifies: bool, route_suppressed: bool, route_reason: str = "",
                                     diagnostics: dict | None = None) -> None:
    now_mono = time.monotonic()
    icao = str(getattr(aircraft, "icao24", "") or "").lower().strip()
    if not icao:
        return
    signature = "|".join((str(getattr(prediction, "state", "") or ""), str(getattr(prediction, "confidence", "") or ""), "1" if qualifies else "0", "1" if route_suppressed else "0", str(route_reason or "")[:100]))
    throttle_key = (int(user_id), icao, hashlib.sha1(signature.encode("utf-8")).hexdigest()[:12])
    previous = _last_snapshot.get(throttle_key, 0.0)
    if now_mono - previous < _SNAPSHOT_INTERVAL_S:
        return
    _last_snapshot[throttle_key] = now_mono
    _prune_throttle(now_mono)

    now = datetime.now(timezone.utc)
    t_cpa = _safe_float(getattr(prediction, "time_to_cpa_s", None))
    doc = {
        "kind": "prediction", "case_id": _case_id(user_id, icao, now), "release_version": VERSION,
        "prediction_version": PREDICTION_VERSION, "shadow_feature_flags": feature_flags(),
        "diagnostics": diagnostics or {}, "captured_at": now, "expires_at": now + timedelta(days=8),
        "user_id": int(user_id), "aircraft_icao24": icao,
        "callsign": str(getattr(aircraft, "callsign", "") or "").strip(),
        "aircraft_type": str(getattr(aircraft, "aircraft_type", "") or ""),
        "trajectory_observation": {
            "latitude": _safe_float(getattr(aircraft, "latitude", None)), "longitude": _safe_float(getattr(aircraft, "longitude", None)),
            "altitude_m": _safe_float(getattr(aircraft, "altitude", None)), "heading_deg": _safe_float(getattr(aircraft, "heading", None)),
            "speed_kts": _safe_float(getattr(aircraft, "ground_speed", None)), "vertical_rate_mps": _safe_float(getattr(aircraft, "vertical_rate_mps", None)),
            "position_age_s": _safe_float(getattr(aircraft, "position_age_s", None)),
        },
        "current_distance_km": _safe_float(getattr(prediction, "current_distance_km", None)),
        "current_slant_km": _safe_float(getattr(prediction, "current_slant_km", None)),
        "projected_closest_km": _safe_float(getattr(prediction, "projected_closest_km", None)),
        "projected_closest_3d_km": _safe_float(getattr(prediction, "projected_closest_3d_km", None)),
        "projected_closest_3d_lower_bound_km": _safe_float(getattr(prediction, "projected_closest_3d_lower_bound_km", None)),
        "time_to_cpa_s": t_cpa, "time_to_3d_cpa_s": _safe_float(getattr(prediction, "time_to_3d_cpa_s", None)),
        "horizon_bucket": _horizon_bucket(t_cpa), "state": str(getattr(prediction, "state", "") or ""),
        "decision_reason": str(getattr(prediction, "reason", "") or "")[:500], "confidence": str(getattr(prediction, "confidence", "") or ""),
        "confidence_score": _safe_float(getattr(prediction, "confidence_score", None)), "enters_alert_radius": bool(getattr(prediction, "enters_alert_radius", False)),
        "altitude_relevance_applied": bool(getattr(prediction, "altitude_relevance_applied", False)),
        "altitude_confidence": str(getattr(prediction, "altitude_confidence", "Unavailable") or "Unavailable"),
        "alert_radius_km": float(alert_radius_km), "qualifies": bool(qualifies), "route_suppressed": bool(route_suppressed),
        "route_reason": str(route_reason or "")[:500], "coverage_mode": "regional_adsb_current_predictor", "coverage_missing": False, "shadow_only": False,
    }
    await append_evidence(doc)
    _recent_snapshots[(int(user_id), icao)].append(doc)
    await _legacy_insert("prediction_lab_audit", doc)


async def record_prediction_outcome(*, user_id: int, aircraft: Any, outcome: str, observed_closest_km: float,
                                    final_prediction: Any, observed_closest_at: datetime | None = None,
                                    previous_projected_closest_km: float | None = None,
                                    route_suppressed: bool = False, route_reason: str = "") -> None:
    now = datetime.now(timezone.utc)
    icao = str(getattr(aircraft, "icao24", "") or "").lower().strip()
    if not icao:
        return
    previous_cpa = _safe_float(previous_projected_closest_km)
    observed_pass = str(outcome) == "passed"
    doc = {
        "kind": "outcome", "case_id": _case_id(user_id, icao, now), "release_version": VERSION, "prediction_version": PREDICTION_VERSION,
        "outcome_basis": "observed_in_radius_pass" if observed_pass else "lifecycle_transition_only", "scoreable": bool(observed_pass),
        "captured_at": now, "expires_at": now + timedelta(days=8), "user_id": int(user_id), "aircraft_icao24": icao,
        "callsign": str(getattr(aircraft, "callsign", "") or "").strip(), "aircraft_type": str(getattr(aircraft, "aircraft_type", "") or ""),
        "outcome": str(outcome), "observed_closest_km": float(observed_closest_km), "observed_closest_at": observed_closest_at,
        "previous_projected_closest_km": previous_cpa, "final_projected_closest_km": _safe_float(getattr(final_prediction, "projected_closest_km", None)),
        "final_projected_closest_3d_km": _safe_float(getattr(final_prediction, "projected_closest_3d_km", None)),
        "final_projected_closest_3d_lower_bound_km": _safe_float(getattr(final_prediction, "projected_closest_3d_lower_bound_km", None)),
        "final_time_to_cpa_s": _safe_float(getattr(final_prediction, "time_to_cpa_s", None)), "final_time_to_3d_cpa_s": _safe_float(getattr(final_prediction, "time_to_3d_cpa_s", None)),
        "final_state": str(getattr(final_prediction, "state", "") or ""), "final_confidence": str(getattr(final_prediction, "confidence", "") or ""),
        "final_altitude_relevance_applied": bool(getattr(final_prediction, "altitude_relevance_applied", False)), "route_suppressed": bool(route_suppressed),
        "route_reason": str(route_reason or "")[:500], "coverage_mode": "regional_adsb_current_predictor", "coverage_missing": False, "shadow_only": False,
    }
    await append_evidence(doc)
    await _legacy_insert("prediction_lab_audit", doc)

    if doc["scoreable"]:
        cutoff = now - timedelta(minutes=30)
        snapshots = list(_recent_snapshots.get((int(user_id), icao), ()))
        for snapshot in snapshots:
            captured = snapshot.get("captured_at")
            if not isinstance(captured, datetime) or captured < cutoff:
                continue
            for row in evaluate_snapshot_outcome(snapshot, doc):
                row["kind"] = "shadow_evaluation"
                row["case_id"] = doc["case_id"]
                row["captured_at"] = now
                row["coverage_missing"] = False
                row["shadow_only"] = row.get("model_role") != "production-control"
                await append_evidence(row)
                await _legacy_insert("prediction_shadow_evaluations", row)


def enqueue_snapshot(*, user_id, aircraft, prediction, alert_radius_km, qualifies, route_suppressed, route_reason="", diagnostics=None):
    icao = str(getattr(aircraft, "icao24", "") or "").lower().strip()
    if icao:
        _track_closest_observation(user_id, icao, getattr(prediction, "current_distance_km", None), diagnostics)
    merged_diagnostics = _snapshot_diagnostics(aircraft, prediction, diagnostics)
    pred = SimpleNamespace(**{name: getattr(prediction, name, None) for name in ("time_to_cpa_s", "time_to_3d_cpa_s", "current_distance_km", "current_slant_km", "projected_closest_km", "projected_closest_3d_km", "projected_closest_3d_lower_bound_km", "state", "reason", "confidence", "confidence_score", "enters_alert_radius", "altitude_relevance_applied", "altitude_confidence")})
    ac = SimpleNamespace(**{name: getattr(aircraft, name, None) for name in ("icao24", "callsign", "aircraft_type", "latitude", "longitude", "altitude", "heading", "ground_speed", "vertical_rate_mps", "position_age_s")})
    signature = hashlib.sha1("|".join((str(getattr(pred, "state", "") or ""), str(getattr(pred, "confidence", "") or ""), "1" if qualifies else "0", "1" if route_suppressed else "0", str(route_reason or "")[:100])).encode("utf-8")).hexdigest()[:12]
    audit_work.get(("prediction", user_id, ac.icao24, signature), lambda: record_prediction_snapshot(user_id=user_id, aircraft=ac, prediction=pred, alert_radius_km=alert_radius_km, qualifies=qualifies, route_suppressed=route_suppressed, route_reason=route_reason, diagnostics=merged_diagnostics), ttl=60)


def enqueue_outcome(**kwargs):
    aircraft = kwargs["aircraft"]
    key = (int(kwargs["user_id"]), str(aircraft.icao24).lower().strip())
    tracked = _closest_observation.pop(key, None)
    if tracked is not None:
        observed_closest = _safe_float(kwargs.get("observed_closest_km"))
        tolerance_km = max(0.05, (observed_closest or 0.0) * 0.01)
        if observed_closest is not None and abs(tracked[0] - observed_closest) <= tolerance_km:
            kwargs = {**kwargs, "observed_closest_at": tracked[1]}
    audit_work.get(("outcome", kwargs["user_id"], aircraft.icao24, kwargs["outcome"]), lambda: record_prediction_outcome(**kwargs), ttl=30)
