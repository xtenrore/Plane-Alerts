"""Plane Alerts v5.2 deterministic shadow-model evaluation.

The evaluator is deliberately outside the alert-critical path. It compares the
already-selected production prediction with shadow-only candidate predictions,
then scores them only when a later outcome carries explicit scoreable ground
truth. Nothing in this module can promote a model or affect an alert.
"""
from __future__ import annotations

import hashlib
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any, Iterable

from pymongo import UpdateOne

from app.version import PREDICTION_VERSION, VERSION

EVALUATION_SCHEMA = "plane-alerts-shadow-evaluation-v52"
EVALUATION_COLLECTION = "prediction_shadow_evaluations"
EVALUATION_TTL_DAYS = 14
LIVE_LOOKBACK_MINUTES = 30
MAX_LIVE_SNAPSHOTS = 24
DEFAULT_MIN_PROMOTION_SAMPLES = 50


def _flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def feature_flags() -> dict[str, bool]:
    """Return shadow-only feature flags. These flags never alter live alerts."""
    return {
        "evaluation_enabled": _flag("PLANE_SHADOW_EVALUATION_ENABLED", True),
        "v46_linear_enabled": _flag("PLANE_SHADOW_V46_LINEAR_ENABLED", True),
        "v46_turn_enabled": _flag("PLANE_SHADOW_V46_TURN_ENABLED", True),
    }


def _snapshot_flags(snapshot: dict[str, Any]) -> dict[str, bool]:
    """Use the flags that were in force when the prediction was captured."""
    current = feature_flags()
    stored = snapshot.get("shadow_feature_flags")
    if not isinstance(stored, dict):
        return current
    return {
        key: bool(stored.get(key, current[key]))
        for key in current
    }


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


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


def _control_model(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": str(snapshot.get("prediction_version") or PREDICTION_VERSION),
        "role": "production-control",
        "cpa_km": _number(snapshot.get("projected_closest_km")),
        "eta_s": _number(snapshot.get("time_to_cpa_s")),
        "enters_radius": bool(snapshot.get("enters_alert_radius")),
        "confidence_score": _number(snapshot.get("confidence_score")),
    }


def _shadow_models(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    flags = _snapshot_flags(snapshot)
    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    v46 = diagnostics.get("v46") if isinstance(diagnostics.get("v46"), dict) else {}
    out: list[dict[str, Any]] = []
    for key, flag_name, suffix in (
        ("linear_shadow", "v46_linear_enabled", "linear"),
        ("turn_shadow", "v46_turn_enabled", "turn-aware"),
    ):
        if not flags[flag_name]:
            continue
        raw = v46.get(key) if isinstance(v46.get(key), dict) else None
        if not raw:
            continue
        version = str(raw.get("model_version") or "v46-shadow")
        mode = str(raw.get("mode") or suffix)
        out.append({
            "model_id": f"{version}:{mode}",
            "role": "shadow-candidate",
            "cpa_km": _number(raw.get("cpa_km")),
            "eta_s": _number(raw.get("eta_s")),
            "enters_radius": bool(raw.get("enters_radius")),
            "confidence_score": None,
        })
    return out


def models_for_snapshot(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    return [_control_model(snapshot), *_shadow_models(snapshot)]


def _scoreable_truth(snapshot: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any] | None:
    if not bool(outcome.get("scoreable")):
        return None
    observed = _number(outcome.get("observed_closest_km"))
    radius = _number(snapshot.get("alert_radius_km"))
    captured = _utc(snapshot.get("captured_at"))
    closest_at = _utc(outcome.get("observed_closest_at"))
    if observed is None or radius is None or captured is None:
        return None

    # ETA truth must come from the timestamp of the observed closest point.
    # Lifecycle resolution can happen later and is not a physical CPA time.
    actual_eta = None
    eta_basis = "unavailable"
    if closest_at is not None:
        actual_eta = (closest_at - captured).total_seconds()
        if actual_eta < 0 or actual_eta > 3600:
            actual_eta = None
        else:
            eta_basis = "observed_closest_at"

    outcome_type = str(outcome.get("outcome") or "")
    actual_positive = observed <= radius
    if outcome_type == "passed" and not actual_positive:
        # A "passed" lifecycle record outside the configured radius is not
        # sufficient ground truth for false-positive/negative claims.
        return None
    return {
        "observed_closest_km": observed,
        "actual_eta_s": actual_eta,
        "actual_eta_basis": eta_basis,
        "actual_positive": actual_positive,
        "outcome": outcome_type,
        "outcome_basis": str(outcome.get("outcome_basis") or ""),
    }


def evaluate_snapshot_outcome(snapshot: dict[str, Any], outcome: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate production and shadow predictions against one scoreable outcome."""
    flags = _snapshot_flags(snapshot)
    if not flags["evaluation_enabled"]:
        return []
    truth = _scoreable_truth(snapshot, outcome)
    if truth is None:
        return []

    snapshot_at = _utc(snapshot.get("captured_at"))
    release_version = str(snapshot.get("release_version") or snapshot.get("plane_version") or "pre-v5.2-unversioned")
    source_id = str(snapshot.get("_id") or snapshot.get("record_id") or "")
    outcome_id = str(outcome.get("_id") or outcome.get("record_id") or "")
    rows: list[dict[str, Any]] = []
    for model in models_for_snapshot(snapshot):
        cpa = _number(model.get("cpa_km"))
        eta = _number(model.get("eta_s"))
        predicted_positive = bool(model.get("enters_radius"))
        actual_positive = bool(truth["actual_positive"])
        classification_correct = predicted_positive == actual_positive
        confidence = _number(model.get("confidence_score"))
        evaluation_key = "|".join((source_id, outcome_id, str(model["model_id"]), str(snapshot_at)))
        row = {
            "schema": EVALUATION_SCHEMA,
            "evaluation_id": hashlib.sha256(evaluation_key.encode()).hexdigest()[:32],
            "release_version": release_version,
            "production_prediction_version": str(snapshot.get("prediction_version") or PREDICTION_VERSION),
            "model_id": str(model["model_id"]),
            "model_role": str(model["role"]),
            "aircraft_icao24": str(snapshot.get("aircraft_icao24") or ""),
            "user_id": snapshot.get("user_id"),
            "snapshot_at": snapshot_at,
            "outcome_at": _utc(outcome.get("captured_at")),
            "horizon_bucket": str(snapshot.get("horizon_bucket") or _horizon_bucket(eta)),
            "coverage_mode": str(snapshot.get("coverage_mode") or "unknown"),
            "shadow_feature_flags": flags,
            "predicted_cpa_km": cpa,
            "observed_closest_km": truth["observed_closest_km"],
            "cpa_error_km": abs(cpa - truth["observed_closest_km"]) if cpa is not None else None,
            "predicted_eta_s": eta,
            "actual_eta_s": truth["actual_eta_s"],
            "actual_eta_basis": truth["actual_eta_basis"],
            "eta_error_s": abs(eta - truth["actual_eta_s"]) if eta is not None and truth["actual_eta_s"] is not None else None,
            "predicted_positive": predicted_positive,
            "actual_positive": actual_positive,
            "classification_correct": classification_correct,
            "false_positive": predicted_positive and not actual_positive,
            "false_negative": (not predicted_positive) and actual_positive,
            "alert_lead_time_s": truth["actual_eta_s"] if predicted_positive else None,
            "confidence_score": confidence,
            # v4.6 confidence describes reliability of the prediction, not the
            # probability that the aircraft will pass. Calibrate it against
            # classification correctness rather than against pass/no-pass.
            "confidence_brier": (confidence - (1.0 if classification_correct else 0.0)) ** 2 if confidence is not None else None,
            "outcome": truth["outcome"],
            "outcome_basis": truth["outcome_basis"],
            # Cancellation accuracy is deliberately unavailable until the
            # lifecycle emits a separately coverage-validated resolved negative.
            "cancellation_scoreable": truth["outcome"] in {"cancelled_resolved", "resolved_negative"},
            "cancellation_correct": (
                (not predicted_positive) and (not actual_positive)
                if truth["outcome"] in {"cancelled_resolved", "resolved_negative"}
                else None
            ),
            "shadow_only": model["role"] != "production-control",
            "automatic_promotion_allowed": False,
        }
        rows.append(row)
    return rows


def _avg(values: Iterable[Any]) -> float | None:
    nums = [value for item in values if (value := _number(item)) is not None]
    return round(mean(nums), 4) if nums else None


def summarize_evaluations(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Return model/release metrics without inventing missing denominators."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("release_version") or "unknown"), str(row.get("model_id") or "unknown"))].append(row)

    models: list[dict[str, Any]] = []
    for (release, model_id), items in sorted(grouped.items()):
        actual_pos = sum(bool(item.get("actual_positive")) for item in items)
        actual_neg = len(items) - actual_pos
        fp = sum(bool(item.get("false_positive")) for item in items)
        fn = sum(bool(item.get("false_negative")) for item in items)
        cancellation_rows = [item for item in items if bool(item.get("cancellation_scoreable"))]
        cancellation_correct = sum(bool(item.get("cancellation_correct")) for item in cancellation_rows)
        horizons: dict[str, int] = defaultdict(int)
        coverage: dict[str, int] = defaultdict(int)
        eta_bases: dict[str, int] = defaultdict(int)
        for item in items:
            horizons[str(item.get("horizon_bucket") or "unknown")] += 1
            coverage[str(item.get("coverage_mode") or "unknown")] += 1
            if item.get("actual_eta_s") is not None:
                eta_bases[str(item.get("actual_eta_basis") or "unknown")] += 1
        models.append({
            "release_version": release,
            "model_id": model_id,
            "model_role": str(items[0].get("model_role") or "unknown"),
            "samples": len(items),
            "positive_samples": actual_pos,
            "negative_samples": actual_neg,
            "cpa_mae_km": _avg(item.get("cpa_error_km") for item in items),
            "eta_mae_s": _avg(item.get("eta_error_s") for item in items),
            "false_positive_rate": round(fp / actual_neg, 4) if actual_neg else None,
            "false_negative_rate": round(fn / actual_pos, 4) if actual_pos else None,
            "false_positive_count": fp,
            "false_negative_count": fn,
            "cancellation_accuracy": round(cancellation_correct / len(cancellation_rows), 4) if cancellation_rows else None,
            "resolved_cancellations": len(cancellation_rows),
            "alert_lead_time_mean_s": _avg(item.get("alert_lead_time_s") for item in items),
            "confidence_brier_mean": _avg(item.get("confidence_brier") for item in items),
            "horizon_counts": dict(sorted(horizons.items())),
            "coverage_modes": dict(sorted(coverage.items())),
            "eta_basis_counts": dict(sorted(eta_bases.items())),
        })
    return {
        "schema": EVALUATION_SCHEMA,
        "models": models,
        "note": (
            "Rates are reported only when their denominator exists. Missing ADS-B coverage and unresolved cancellations "
            "are not converted into successes or misses. Shadow candidates never control alerts."
        ),
    }


def promotion_assessment(
    candidate_metrics: dict[str, Any],
    control_metrics: dict[str, Any],
    *,
    replay_success: bool,
    live_shadow_success: bool,
    min_samples: int = DEFAULT_MIN_PROMOTION_SAMPLES,
) -> dict[str, Any]:
    """Assess evidence sufficiency. This function never promotes a model."""
    samples = int(candidate_metrics.get("samples") or 0)
    horizons = candidate_metrics.get("horizon_counts") if isinstance(candidate_metrics.get("horizon_counts"), dict) else {}
    represented = sum(1 for count in horizons.values() if int(count or 0) >= 5)
    safety_regression = False
    safety_rates_comparable = True
    for key in ("false_positive_rate", "false_negative_rate"):
        candidate = _number(candidate_metrics.get(key))
        control = _number(control_metrics.get(key))
        if candidate is None or control is None:
            safety_rates_comparable = False
            continue
        if candidate > control + 0.02:
            safety_regression = True
    requirements = {
        "enough_samples": samples >= max(1, int(min_samples)),
        "representative_coverage": represented >= 2,
        "safety_rates_comparable": safety_rates_comparable,
        "no_major_safety_regression": safety_rates_comparable and not safety_regression,
        "replay_success": bool(replay_success),
        "live_shadow_success": bool(live_shadow_success),
    }
    return {
        "requirements": requirements,
        "eligible_for_manual_review": all(requirements.values()),
        "automatic_promotion": False,
        "decision": "hold-for-human-review" if all(requirements.values()) else "hold-insufficient-evidence",
        "rule": "No candidate is automatically promoted because one metric improves.",
    }


async def evaluate_live_outcome(db: Any, outcome: dict[str, Any]) -> int:
    """Join one scoreable live outcome to recent snapshots and persist bounded evaluations."""
    if not feature_flags()["evaluation_enabled"] or not bool(outcome.get("scoreable")):
        return 0
    captured = _utc(outcome.get("captured_at"))
    if captured is None:
        return 0
    closest_at = _utc(outcome.get("observed_closest_at"))
    lookup_at = closest_at or captured
    query = {
        "kind": "prediction",
        "user_id": outcome.get("user_id"),
        "aircraft_icao24": str(outcome.get("aircraft_icao24") or ""),
        "captured_at": {"$gte": lookup_at - timedelta(minutes=LIVE_LOOKBACK_MINUTES), "$lte": lookup_at},
    }
    cursor = db["prediction_lab_audit"].find(query).sort("captured_at", -1).limit(MAX_LIVE_SNAPSHOTS)
    snapshots = [dict(doc) async for doc in cursor]
    now = datetime.now(timezone.utc)
    operations: list[UpdateOne] = []
    for snapshot in snapshots:
        for row in evaluate_snapshot_outcome(snapshot, outcome):
            row["evaluated_at"] = now
            row["expires_at"] = now + timedelta(days=EVALUATION_TTL_DAYS)
            operations.append(
                UpdateOne(
                    {"evaluation_id": row["evaluation_id"]},
                    {"$set": row},
                    upsert=True,
                )
            )
    if not operations:
        return 0
    # One bounded Mongo command per resolved outcome avoids turning a scoreable
    # pass into dozens of sequential optional writes.
    await db[EVALUATION_COLLECTION].bulk_write(operations, ordered=False)
    return len(operations)


def evaluate_replay_cases(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Pure deterministic replay evaluator used by CI and offline tooling."""
    rows: list[dict[str, Any]] = []
    skipped = 0
    input_cases = 0
    for case in cases:
        input_cases += 1
        evaluated = evaluate_snapshot_outcome(dict(case.get("snapshot") or {}), dict(case.get("outcome") or {}))
        if not evaluated:
            skipped += 1
        rows.extend(evaluated)
    summary = summarize_evaluations(rows)
    summary["input_cases"] = input_cases
    summary["evaluation_rows"] = len(rows)
    summary["skipped_unscoreable_cases"] = skipped
    return summary
