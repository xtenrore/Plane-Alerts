"""Plane Alerts v5.0 operator observability and deterministic explanations.

This module is deliberately read-only and outside the alert-critical path. It
reuses bounded telemetry that Plane Alerts already records instead of adding a
second prediction implementation or synchronous diagnostic writes to the live
five-second loop.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.database import close_db, connect_db
from app.version import PREDICTION_VERSION, VERSION

_MAX_RECORDS = 50
_SAFE_PROVIDER_FIELDS = {
    "name",
    "is_unlimited",
    "request_count",
    "error_count",
    "timeout_count",
    "rate_limit_count",
    "malformed_response_count",
    "last_request_time",
    "last_success_time",
    "last_error",
    "last_latency_ms",
    "latency_p50_ms",
    "latency_p95_ms",
    "last_aircraft_count",
    "recent_requests",
    "recent_failures",
    "stale_position_rate",
    "unknown_freshness_count",
    "consecutive_failures",
    "circuit_state",
    "cooldown_remaining_s",
    "can_request_now",
    "configured",
    "receiver_type",
    "endpoint_resolved",
}


def _user_ref(value: Any) -> str:
    return "u-" + hashlib.sha256(f"plane-alerts-v5:{value}".encode()).hexdigest()[:10]


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _provider_status_safe(raw: dict[str, Any]) -> dict[str, Any]:
    """Return only bounded provider-health fields; never endpoints/credentials."""
    return {key: _json_safe(raw.get(key)) for key in _SAFE_PROVIDER_FIELDS if key in raw}


def _nested(mapping: Any, *keys: str) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def explain_prediction(row: dict[str, Any]) -> dict[str, Any]:
    """Turn one persisted prediction snapshot into a deterministic why/why-not view."""
    diagnostics = row.get("diagnostics") if isinstance(row.get("diagnostics"), dict) else {}
    observation = diagnostics.get("observation") if isinstance(diagnostics.get("observation"), dict) else {}
    radius = _number(row.get("alert_radius_km"))
    current = _number(row.get("current_distance_km"))
    cpa = _number(row.get("projected_closest_km"))
    eta = _number(row.get("time_to_cpa_s"))
    confidence = _number(row.get("confidence_score"))
    route_suppressed = bool(row.get("route_suppressed"))
    qualifies = bool(row.get("qualifies"))

    if qualifies:
        decision = "alert-qualified"
    elif route_suppressed:
        decision = "route-or-terminal-suppressed"
    else:
        decision = "not-qualified"

    reasons: list[str] = []
    if cpa is not None and radius is not None:
        if cpa <= radius:
            reasons.append(f"projected CPA {cpa:.2f} km is inside the {radius:.2f} km alert radius")
        else:
            reasons.append(f"projected CPA {cpa:.2f} km is outside the {radius:.2f} km alert radius")
    state = str(row.get("state") or "unknown")
    reasons.append(f"trajectory state is {state}")
    if confidence is not None:
        reasons.append(f"prediction confidence score is {confidence:.3f} ({row.get('confidence') or 'unlabelled'})")
    route_reason = str(row.get("route_reason") or "").strip()
    if route_suppressed:
        reasons.append(f"route/terminal guard suppressed the alert: {route_reason or 'guard evidence'}")
    elif route_reason:
        reasons.append(f"route/terminal evidence: {route_reason}")
    sample_age = _number(diagnostics.get("sample_age_s"))
    if sample_age is not None:
        reasons.append(f"latest accepted position age was {sample_age:.1f}s")
    if diagnostics.get("fresh_observation") is False:
        reasons.append("the cycle did not contain a newer accepted observation")

    route = {
        "destination": diagnostics.get("route_destination"),
        "history_days": diagnostics.get("route_history_days"),
        "similar_days": diagnostics.get("route_similar_days"),
        "similarity_km": diagnostics.get("route_similarity_km"),
        "expected_turn_pending": diagnostics.get("route_expected_turn_pending"),
        "qualification_state": diagnostics.get("qualification_state"),
        "terminal_arrival_state": diagnostics.get("terminal_arrival_state"),
        "expected_turn_state": diagnostics.get("expected_turn_state"),
        "airport_path_cpa_km": diagnostics.get("airport_path_cpa_km"),
        "runway_candidate": _nested(diagnostics, "v47", "runway_candidate"),
        "runway_confidence": _nested(diagnostics, "v47", "runway_confidence"),
    }
    route = {key: _json_safe(value) for key, value in route.items() if value is not None and value != ""}

    return {
        "captured_at": _json_safe(row.get("captured_at")),
        "aircraft": {
            "icao24": str(row.get("aircraft_icao24") or ""),
            "callsign": str(row.get("callsign") or ""),
            "aircraft_type": str(row.get("aircraft_type") or ""),
        },
        "user_ref": _user_ref(row.get("user_id")) if row.get("user_id") is not None else None,
        "decision": decision,
        "why": reasons,
        "trajectory": {
            "state": state,
            "current_distance_km": current,
            "projected_closest_km": cpa,
            "time_to_cpa_s": eta,
            "enters_alert_radius": bool(row.get("enters_alert_radius")),
            "confidence": row.get("confidence"),
            "confidence_score": confidence,
            "turn_rate_deg_s": _number(diagnostics.get("turn_rate_deg_s")),
            "sample_count": diagnostics.get("sample_count"),
            "sample_age_s": sample_age,
        },
        "observation": {
            "active_source": observation.get("source"),
            "candidate_sources": observation.get("source_candidates") or [],
            "data_quality": observation.get("data_quality"),
            "field_provenance": observation.get("field_provenance") or {},
            "merge_notes": observation.get("merge_notes") or [],
        },
        "route_and_terminal": route,
        "prediction_version": row.get("prediction_version") or PREDICTION_VERSION,
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _cursor_rows(cursor: Any) -> list[dict[str, Any]]:
    return [dict(doc) async for doc in cursor]


async def _latest_predictions(
    db: Any,
    *,
    aircraft: str = "",
    callsign: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"kind": "prediction"}
    if aircraft:
        query["aircraft_icao24"] = aircraft.lower().strip()
    if callsign:
        query["callsign"] = callsign.upper().strip()
    projection = {
        "user_id": 1,
        "aircraft_icao24": 1,
        "callsign": 1,
        "aircraft_type": 1,
        "captured_at": 1,
        "prediction_version": 1,
        "current_distance_km": 1,
        "projected_closest_km": 1,
        "time_to_cpa_s": 1,
        "state": 1,
        "confidence": 1,
        "confidence_score": 1,
        "enters_alert_radius": 1,
        "alert_radius_km": 1,
        "qualifies": 1,
        "route_suppressed": 1,
        "route_reason": 1,
        "diagnostics": 1,
    }
    cursor = db["prediction_lab_audit"].find(query, projection).sort("captured_at", -1).limit(max(1, min(_MAX_RECORDS, int(limit))))
    return await asyncio.wait_for(_cursor_rows(cursor), timeout=1.5)


async def _count(db: Any, collection: str, query: dict[str, Any]) -> int:
    return int(await asyncio.wait_for(db[collection].count_documents(query), timeout=1.0))


async def collect_operator_metrics(db: Any) -> dict[str, Any]:
    """Read the latest persisted runtime metrics without touching the live worker."""
    started = time.monotonic()
    monitor_doc = await asyncio.wait_for(db["system_status"].find_one({"_id": "monitor_worker"}), timeout=1.0)
    query_latency_ms = round((time.monotonic() - started) * 1000.0, 1)
    monitor_doc = dict(monitor_doc or {})
    provider_health = [
        _provider_status_safe(dict(item))
        for item in (monitor_doc.get("provider_health_v50") or [])
        if isinstance(item, dict)
    ]
    runtime = monitor_doc.get("runtime_metrics_v50") if isinstance(monitor_doc.get("runtime_metrics_v50"), dict) else {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    (
        sampled_predictions,
        lifecycle_outcomes,
        first_alert_records,
        cancellation_records,
        failed_delivery_records,
    ) = await asyncio.gather(
        _count(db, "prediction_lab_audit", {"kind": "prediction", "captured_at": {"$gte": cutoff}}),
        _count(db, "prediction_lab_audit", {"kind": "outcome", "captured_at": {"$gte": cutoff}}),
        _count(db, "notification_history", {"first_notified_at": {"$gte": cutoff}}),
        _count(
            db,
            "notification_history",
            {"events": {"$elemMatch": {"event_type": "cancellation_update", "occurred_at": {"$gte": cutoff}}}},
        ),
        _count(
            db,
            "notification_history",
            {"events": {"$elemMatch": {"event_type": "failed_delivery", "occurred_at": {"$gte": cutoff}}}},
        ),
    )
    return {
        "version": VERSION,
        "prediction_version": PREDICTION_VERSION,
        "database_probe_latency_ms": query_latency_ms,
        "worker": {
            key: _json_safe(monitor_doc.get(key))
            for key in (
                "plane_version",
                "plane_commit",
                "last_cycle_time",
                "last_cycle_duration_ms",
                "total_cycles",
                "active_users",
                "notifications_sent_last_cycle",
                "shared_regions_last_cycle",
                "provider_queries_last_cycle",
                "shared_snapshot_cache_hits_last_cycle",
                "priority_users_last_cycle",
                "deferred_users_last_cycle",
                "notifications_paused_last_cycle",
                "polling_mode",
                "updated_at",
            )
            if key in monitor_doc
        },
        "providers": provider_health,
        "runtime_metrics": _json_safe(runtime),
        "delivery_24h": {
            "notification_records_first_delivered": first_alert_records,
            "notification_records_with_cancellation_update": cancellation_records,
            "notification_records_with_failed_delivery": failed_delivery_records,
            "note": "Counts are durable notification records with matching events, not a reconstruction of missing telemetry.",
        },
        "prediction_lab_24h": {
            "sampled_predictions": sampled_predictions,
            "lifecycle_outcomes": lifecycle_outcomes,
            "note": "Prediction Lab snapshots are deliberately rate-limited and are not a count of every five-second prediction calculation.",
        },
    }


async def collect_diagnostics(db: Any, *, aircraft: str = "", callsign: str = "", limit: int = 10) -> dict[str, Any]:
    rows = await _latest_predictions(db, aircraft=aircraft, callsign=callsign, limit=limit)
    metrics = await collect_operator_metrics(db)
    return {
        "version": VERSION,
        "prediction_version": PREDICTION_VERSION,
        "selector": {"aircraft": aircraft.lower().strip() or None, "callsign": callsign.upper().strip() or None},
        "records": [explain_prediction(row) for row in rows],
        "metrics": metrics,
        "privacy": "No credentials or precise observer coordinates are returned. User IDs are pseudonymized.",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="planealerts", description="Plane Alerts v5 operator observability")
    sub = parser.add_subparsers(dest="command", required=True)
    diagnostics = sub.add_parser("diagnostics", help="Explain recent per-aircraft alert decisions")
    diagnostics.add_argument("--aircraft", default="", help="ICAO24 hex identifier")
    diagnostics.add_argument("--callsign", default="", help="Flight callsign")
    diagnostics.add_argument("--limit", type=int, default=10, help="Recent records to show (1-50)")
    sub.add_parser("metrics", help="Show provider/runtime/storage/timing metrics")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    db = await connect_db(max_retries=1, retry_delay=0.0, timeout_ms=1500, ensure_indexes=False)
    try:
        if args.command == "metrics":
            return await collect_operator_metrics(db)
        return await collect_diagnostics(db, aircraft=args.aircraft, callsign=args.callsign, limit=args.limit)
    finally:
        await close_db()


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        payload = asyncio.run(_run(args))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "unavailable",
                    "error": type(exc).__name__,
                    "detail": "Operator diagnostics could not reach bounded telemetry storage; live Plane Alerts monitoring is independent.",
                },
                indent=2,
            )
        )
        return 2
    print(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
