"""Sanitized data bridge between production truth, Antigravity, and ChatGPT.

The parent AGY worker owns MongoDB credentials.  The Antigravity child process
never receives them.  Instead this module materializes a small, redacted JSON
snapshot on the persistent volume before each goal run.

Findings travel in the opposite direction: AGY appends to findings.jsonl, then
the parent worker immediately upserts each new finding into MongoDB and emits a
single-line CHATGPT_HANDOFF_JSON marker into Railway logs.  ChatGPT scheduled
tasks can therefore consume findings independently of AGY's hourly boundaries.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, DESCENDING

logger = logging.getLogger("plane_alerts.agy_bridge")

STATE_DIR = Path(os.getenv("AGY_STATE_DIR", "/agy-state"))
LAB_DIR = STATE_DIR / "prediction-lab"
CONTEXT_DIR = LAB_DIR / "context"
CONTEXT_FILE = CONTEXT_DIR / "latest.json"
FINDINGS_FILE = LAB_DIR / "findings.jsonl"
HANDOFF_CURSOR_FILE = LAB_DIR / "handoff-cursor.json"

_MONGO_URI = os.getenv("MONGO_URI", "").strip()
_DB_NAME = os.getenv("DATABASE_NAME", "aircraft_bot").strip() or "aircraft_bot"
_client: MongoClient | None = None

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


def _db():
    global _client
    if not _MONGO_URI:
        return None
    if _client is None:
        _client = MongoClient(
            _MONGO_URI,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=8000,
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
            # Route geometry itself is unnecessary for AGY's audit.  Preserve
            # only how much history exists so the model can reason about data
            # sufficiency without receiving location traces.
            out["point_count"] = len(value)
            continue
        out[key] = _json_value(value)
    return out


def _recent(collection: str, sort_field: str, limit: int, projection: dict[str, int] | None = None) -> list[dict[str, Any]]:
    database = _db()
    if database is None:
        return []
    try:
        cursor = database[collection].find({}, projection).sort(sort_field, DESCENDING).limit(limit)
        return [_sanitize(dict(doc)) for doc in cursor]
    except Exception:
        logger.exception("AGY bridge read failed collection=%s", collection)
        return []


def build_context_snapshot() -> dict[str, Any]:
    """Create the redacted truth snapshot AGY is allowed to inspect."""
    now = datetime.now(timezone.utc)
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
        ],
        "summary": {
            "approach_state_records": len(approach),
            "active_alerts": len(active),
            "cancelled_alerts": len(cancelled),
            "passed_alerts": len(passed),
            "records_with_eta": len(eta_samples),
            "prediction_audit_records": len(audit),
            "audit_horizon_counts": horizon_counts,
            "route_history_records": len(route_history),
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
        "AGY_CONTEXT_READY approach=%d cancelled=%d passed=%d audit=%d",
        len(approach), len(cancelled), len(passed), len(audit),
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
            except Exception:
                # Do not hide a finding merely because Mongo is temporarily
                # unavailable. Railway logs are the independent durable path.
                logger.exception("AGY finding Mongo handoff failed seq=%s", seq)

        logger.warning("CHATGPT_HANDOFF_JSON %s", json.dumps(clean, ensure_ascii=False, separators=(",", ":")))
        _save_cursor(seq)
        cursor = seq
        published += 1
    return published
