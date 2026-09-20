"""Sanitized data bridge between production truth, Antigravity, and ChatGPT.

The parent AGY worker owns MongoDB credentials. The Antigravity child process
never receives them. Instead this module materializes a small, redacted JSON
snapshot on the persistent volume before each goal run.

Findings travel in the opposite direction: AGY appends to findings.jsonl, then
the parent worker immediately upserts each new finding into MongoDB and emits a
single-line CHATGPT_HANDOFF_JSON marker into Railway logs. ChatGPT scheduled
tasks can therefore consume findings independently of AGY's hourly boundaries.

v5.0 makes Mongo access explicitly non-critical. A slow Atlas read may delay one
bridge refresh briefly, but it opens a bounded circuit and subsequent refreshes
reuse the last-known-good redacted context instead of repeatedly blocking the
handoff loop or emitting traceback storms.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import DESCENDING, MongoClient
from pymongo.errors import PyMongoError

logger = logging.getLogger("plane_alerts.agy_bridge")

STATE_DIR = Path(os.getenv("AGY_STATE_DIR", "/agy-state"))
LAB_DIR = STATE_DIR / "prediction-lab"
CONTEXT_DIR = LAB_DIR / "context"
CONTEXT_FILE = CONTEXT_DIR / "latest.json"
FINDINGS_FILE = LAB_DIR / "findings.jsonl"
HANDOFF_CURSOR_FILE = LAB_DIR / "handoff-cursor.json"

_MONGO_URI = os.getenv("MONGO_URI", "").strip()
_DB_NAME = os.getenv("DATABASE_NAME", "aircraft_bot").strip() or "aircraft_bot"
_MONGO_TIMEOUT_MS = max(250, min(2500, int(os.getenv("AGY_MONGO_TIMEOUT_MS", "1200") or 1200)))
_MONGO_QUERY_MAX_MS = max(100, min(_MONGO_TIMEOUT_MS, int(os.getenv("AGY_MONGO_QUERY_MAX_MS", "900") or 900)))
_MONGO_COOLDOWN_S = max(5.0, min(300.0, float(os.getenv("AGY_MONGO_COOLDOWN_SECONDS", "45") or 45)))
_client: MongoClient | None = None
_mongo_circuit_open_until = 0.0
_mongo_failures = 0
_mongo_last_error = ""
_mongo_last_success = 0.0
_recent_cache: dict[str, list[dict[str, Any]]] = {}
_cache_seeded = False

# Snapshot keys mapped back to their Mongo collections for persisted fallback.
_CONTEXT_COLLECTION_KEYS = {
    "approach_states": "approach_states",
    "notification_history": "notification_history",
    "photo_alert_snapshots": "photo_alert_snapshots",
    "feedback": "feedback",
    "system_health": "system_status",
    "profile_configuration": "alert_profiles",
    "route_history_summary": "flight_route_samples",
    "prediction_audit": "prediction_lab_audit",
}

# Never expose credentials or precise observer/device position in the model
# context. Aircraft/route geometry is also omitted because the audit metrics do
# not need it; this keeps the snapshot compact and privacy-minimal.
_DROP_KEYS = {
    "user_id",
    "observer_latitude",
    "observer_longitude",
    "latitude",
    "longitude",
    "lat",
    "lon",
    "token",
    "password",
    "secret",
    "api_key",
    "mongo_uri",
}


def _close_client() -> None:
    global _client
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = None


def _circuit_open() -> bool:
    return time.monotonic() < _mongo_circuit_open_until


def _record_mongo_success() -> None:
    global _mongo_circuit_open_until, _mongo_failures, _mongo_last_error, _mongo_last_success
    _mongo_circuit_open_until = 0.0
    _mongo_failures = 0
    _mongo_last_error = ""
    _mongo_last_success = time.time()


def _record_mongo_failure(exc: BaseException, operation: str) -> None:
    global _mongo_circuit_open_until, _mongo_failures, _mongo_last_error
    _mongo_failures += 1
    _mongo_last_error = type(exc).__name__
    exponent = min(3, max(0, _mongo_failures - 1))
    cooldown = min(300.0, _MONGO_COOLDOWN_S * (2**exponent))
    _mongo_circuit_open_until = time.monotonic() + cooldown
    _close_client()
    logger.warning(
        "AGY_MONGO_DEGRADED operation=%s error=%s cooldown_s=%.1f cached_context=true",
        operation,
        type(exc).__name__,
        cooldown,
    )


def bridge_status() -> dict[str, Any]:
    remaining = max(0.0, _mongo_circuit_open_until - time.monotonic())
    if not _MONGO_URI:
        state = "unconfigured"
    elif remaining > 0:
        state = "degraded"
    else:
        state = "ready"
    return {
        "mongo_state": state,
        "mongo_failures": int(_mongo_failures),
        "mongo_last_error": _mongo_last_error,
        "mongo_last_success_at": datetime.fromtimestamp(_mongo_last_success, tz=timezone.utc).isoformat() if _mongo_last_success else None,
        "mongo_retry_in_s": round(remaining, 1),
        "read_timeout_ms": _MONGO_TIMEOUT_MS,
        "query_max_ms": _MONGO_QUERY_MAX_MS,
        "cached_collections": len(_recent_cache),
    }


def _db():
    global _client
    if not _MONGO_URI or _circuit_open():
        return None
    if _client is None:
        _client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=_MONGO_TIMEOUT_MS,
            connectTimeoutMS=_MONGO_TIMEOUT_MS,
            socketTimeoutMS=_MONGO_TIMEOUT_MS,
            maxPoolSize=3,
            minPoolSize=0,
            appname="plane-alerts-agy-v5",
        )
    return _client[_DB_NAME]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _user_ref(value: Any) -> str:
    return "u-" + hashlib.sha256(f"plane-alerts:{value}".encode()).hexdigest()[:10]


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value[:200]]
    if isinstance(value, dict):
        return _sanitize(value)
    return str(value)


def _sanitize(doc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "user_id" in doc:
        out["user_ref"] = _user_ref(doc.get("user_id"))
    for raw_key, value in doc.items():
        key = str(raw_key)
        lowered = key.lower()
        if key == "_id":
            out["record_id"] = str(value)
            continue
        if lowered in _DROP_KEYS or any(word in lowered for word in ("password", "secret", "token", "api_key")):
            continue
        if key == "points" and isinstance(value, list):
            # Route geometry itself is unnecessary for AGY's audit. Preserve
            # only how much history exists so the model can reason about data
            # sufficiency without receiving location traces.
            out["point_count"] = len(value)
            continue
        out[key] = _json_value(value)
    return out


def _seed_recent_cache_from_context() -> None:
    global _cache_seeded
    if _cache_seeded:
        return
    _cache_seeded = True
    try:
        payload = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    for snapshot_key, collection in _CONTEXT_COLLECTION_KEYS.items():
        value = payload.get(snapshot_key)
        if isinstance(value, list):
            _recent_cache[collection] = [dict(item) for item in value if isinstance(item, dict)]


def _cached_recent(collection: str, limit: int) -> list[dict[str, Any]]:
    _seed_recent_cache_from_context()
    rows = _recent_cache.get(collection, [])
    return [dict(row) for row in rows[:limit]]


def _recent(collection: str, sort_field: str, limit: int, projection: dict[str, int] | None = None) -> list[dict[str, Any]]:
    database = _db()
    if database is None:
        return _cached_recent(collection, limit)
    try:
        cursor = database[collection].find({}, projection).sort(sort_field, DESCENDING).limit(limit)
        if hasattr(cursor, "max_time_ms"):
            cursor = cursor.max_time_ms(_MONGO_QUERY_MAX_MS)
        rows = [_sanitize(dict(doc)) for doc in cursor]
        _recent_cache[collection] = [dict(row) for row in rows]
        _record_mongo_success()
        return rows
    except PyMongoError as exc:
        _record_mongo_failure(exc, f"read:{collection}")
        return _cached_recent(collection, limit)
    except Exception as exc:
        # Programming/data-shape errors should not create an endless traceback
        # storm either, but they are not treated as proof Atlas is unavailable.
        logger.warning("AGY bridge read failed collection=%s error=%s", collection, type(exc).__name__)
        return _cached_recent(collection, limit)


def build_context_snapshot() -> dict[str, Any]:
    """Create the redacted truth snapshot AGY is allowed to inspect."""
    now = datetime.now(timezone.utc)
    _seed_recent_cache_from_context()
    approach = _recent("approach_states", "updated_at", 350)
    notifications = _recent("notification_history", "notified_at", 350)
    photos = _recent("photo_alert_snapshots", "captured_at", 150)
    feedback = _recent("feedback", "updated_at", 150)
    system = _recent("system_status", "updated_at", 20)
    profiles = _recent("alert_profiles", "updated_at", 100, {"name": 0})
    route_history = _recent(
        "flight_route_samples",
        "updated_at",
        120,
        {"callsign": 1, "utc_date": 1, "updated_at": 1, "points": 1},
    )
    audit = _recent("prediction_lab_audit", "captured_at", 700)

    cancelled = [row for row in approach if row.get("stage") == "cancelled"]
    passed = [row for row in approach if row.get("stage") == "passed"]
    active = [row for row in approach if bool(row.get("active"))]
    eta_samples = [row for row in approach if row.get("time_to_cpa_s") is not None]
    horizon_counts: dict[str, int] = {}
    for row in audit:
        bucket = str(row.get("horizon_bucket") or "unknown")
        horizon_counts[bucket] = horizon_counts.get(bucket, 0) + 1
    mongo_status = bridge_status()

    payload = {
        "schema": "plane-alerts-prediction-lab-v1",
        "generated_at": now.isoformat(),
        "purpose": (
            "Evidence for ETA accuracy, cancelled-alert accuracy, and next-hour spotting expectation-vs-reality audits. "
            "Also inspect Not Helpful feedback, profile/filter patterns, camera guidance, exceptions, provider failures and measured cadence. Findings are hypotheses requiring verified evidence, never production decisions. This is deterministic production telemetry; AI is analysis-only."
        ),
        "limitations": [
            "Current ADS-B acquisition is regional. Sparse 30-60 minute samples mean insufficient coverage, not good forecast accuracy.",
            "The public Next 60 Minutes forecast is not yet trusted; treat long-horizon records as shadow evidence only.",
            "Exact user/observer coordinates and credentials are intentionally removed.",
            "Cancellation is a lifecycle event, not proof of a miss. Only coverage-validated outcomes marked outcome_version 4.4-bounded-observed-pass are suitable for new accuracy claims.",
            "When bridge_status.mongo_state is degraded, collection data may be last-known-good cached evidence until Atlas recovers.",
        ],
        "bridge_status": mongo_status,
        "summary": {
            "approach_state_records": len(approach),
            "active_alerts": len(active),
            "cancelled_alerts": len(cancelled),
            "passed_alerts": len(passed),
            "records_with_eta": len(eta_samples),
            "prediction_audit_records": len(audit),
            "audit_horizon_counts": horizon_counts,
            "route_history_records": len(route_history),
            "mongo_degraded": mongo_status["mongo_state"] == "degraded",
        },
        "approach_states": approach,
        "notification_history": notifications,
        "photo_alert_snapshots": photos,
        "feedback": feedback,
        "system_health": system,
        "profile_configuration": profiles,
        "route_history_summary": route_history,
        "prediction_audit": audit,
    }
    _atomic_json(CONTEXT_FILE, payload)
    logger.info(
        "AGY_CONTEXT_READY approach=%d cancelled=%d passed=%d audit=%d mongo=%s cached=%d",
        len(approach),
        len(cancelled),
        len(passed),
        len(audit),
        mongo_status["mongo_state"],
        mongo_status["cached_collections"],
    )
    return payload


def _load_cursor() -> int:
    try:
        return int(json.loads(HANDOFF_CURSOR_FILE.read_text(encoding="utf-8")).get("seq", 0) or 0)
    except Exception:
        return 0


def _save_cursor(seq: int) -> None:
    _atomic_json(HANDOFF_CURSOR_FILE, {"seq": int(seq), "updated_at": datetime.now(timezone.utc).isoformat()})


def sync_findings_to_handoff() -> int:
    """Publish every newly durable AGY finding for ChatGPT immediately."""
    if not FINDINGS_FILE.exists():
        return 0
    cursor = _load_cursor()
    rows: list[dict[str, Any]] = []
    for raw in FINDINGS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        seq = int(item.get("seq", 0) or 0)
        if seq > cursor:
            rows.append(item)
    rows.sort(key=lambda item: int(item.get("seq", 0) or 0))
    if not rows:
        return 0

    database = _db()
    published = 0
    for item in rows:
        seq = int(item.get("seq", 0) or 0)
        clean = _sanitize(dict(item))
        clean["seq"] = seq
        clean["chatgpt_status"] = "new"
        clean["handoff_at"] = datetime.now(timezone.utc).isoformat()
        clean["handoff_source"] = "Plane-Alerts-AGY"

        if database is not None:
            try:
                database["agy_findings"].update_one(
                    {"finding_id": item.get("finding_id")},
                    {"$set": {**item, "synced_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                database["chatgpt_handoff"].update_one(
                    {"source": "Plane-Alerts-AGY", "seq": seq},
                    {"$set": {**clean, "source": "Plane-Alerts-AGY"}},
                    upsert=True,
                )
                _record_mongo_success()
            except PyMongoError as exc:
                # Railway logs are an independent durable handoff path. Open a
                # circuit after the first timeout so the next rows do not each
                # pay another socket timeout or print another traceback.
                _record_mongo_failure(exc, "finding-handoff")
                database = None
            except Exception as exc:
                logger.warning("AGY finding Mongo handoff failed seq=%s error=%s", seq, type(exc).__name__)

        logger.warning("CHATGPT_HANDOFF_JSON %s", json.dumps(clean, ensure_ascii=False, separators=(",", ":")))
        _save_cursor(seq)
        cursor = seq
        published += 1
    return published


def _reset_resilience_state_for_tests() -> None:
    """Reset module-local resilience state; never used by production runtime."""
    global _mongo_circuit_open_until, _mongo_failures, _mongo_last_error, _mongo_last_success, _cache_seeded
    _close_client()
    _mongo_circuit_open_until = 0.0
    _mongo_failures = 0
    _mongo_last_error = ""
    _mongo_last_success = 0.0
    _recent_cache.clear()
    _cache_seeded = False
