"""Read-only, bounded deterministic evidence collector for shadow audits.

The input adapter supplies already sanitized Prediction Lab evidence records.
This module never runs a model and never influences the live alert path.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .store import Store

_REF = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_KINDS = frozenset({"prediction", "outcome", "alert", "provider", "coverage", "trajectory"})
MAX_EVENTS = 2048
MAX_PACKETS = 64
MAX_BACKLOG_HOURS = 24


@dataclass(frozen=True)
class WindowResult:
    packets: int
    events: int
    overflow: bool
    checkpoint: int


def due_hours(store: Store, *, now: int, settle_seconds: int = 3600) -> list[tuple[int, int]]:
    """Bounded hourly schedule; old gaps require explicit recovery review."""
    if settle_seconds < 300 or settle_seconds > 86400:
        raise ValueError("invalid source settling delay")
    closed_end = (now - settle_seconds) // 3600 * 3600
    row = store.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='collector_hour'").fetchone()
    start = int(row[0]) if row else closed_end - 3600
    if start > closed_end or closed_end - start > MAX_BACKLOG_HOURS * 3600:
        raise ValueError("collector backlog needs explicit recovery")
    return [(at, at + 3600) for at in range(start, closed_end, 3600)]


def _ref(value: object) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ValueError("invalid sanitized evidence reference")
    return value


def _metric(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not -100000 <= value <= 100000:
        raise ValueError("invalid bounded evidence metric")
    return round(float(value), 3)


def _event(raw: Mapping[str, object], start: int, end: int) -> dict:
    at = raw.get("at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not start <= at < end:
        raise ValueError("event outside audit window")
    kind = raw.get("kind")
    if kind not in _KINDS:
        raise ValueError("unknown evidence kind")
    return {"event_id": _ref(raw.get("event_id")), "case_ref": _ref(raw.get("case_ref")),
            "at": round(float(at), 3), "kind": kind,
            "state": str(raw.get("state", ""))[:40] if raw.get("state") in
                ("APPROACHING", "NOT_APPROACHING", "QUALIFIED", "CANCELLED", "HELD", "OBSERVED", "") else "",
            "cpa_km": _metric(raw.get("cpa_km")), "observed_km": _metric(raw.get("observed_km")),
            "coverage": raw.get("coverage") if raw.get("coverage") in ("good", "partial", "missing") else "missing"}


def _anomalies(events: list[dict]) -> list[str]:
    kinds = {e["kind"] for e in events}
    states = [e["state"] for e in events if e["state"]]
    reasons = []
    if "CANCELLED" in states:
        reasons.append("cancellation")
    if "CANCELLED" in states and any(s in ("QUALIFIED", "APPROACHING") for s in states[states.index("CANCELLED")+1:]):
        reasons.append("requalification_after_cancellation")
    if states.count("CANCELLED") > 1:
        reasons.append("qualify_cancel_oscillation")
    for kind in ("trajectory", "provider", "coverage"):
        if kind in kinds:
            reasons.append(kind + "_changed")
    if any(e["coverage"] == "missing" for e in events):
        reasons.append("missing_coverage")
    return reasons


def adapt_prediction_lab_record(raw: Mapping[str, object]) -> dict:
    """Allowlist only sanitized Prediction Lab fields; discard raw coordinates."""
    captured = raw.get("captured_at")
    if not isinstance(captured, str):
        raise ValueError("missing source timestamp")
    try:
        at = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError("timestamp must include timezone")
    except (ValueError, OverflowError):
        raise ValueError("invalid source timestamp") from None
    kind = raw.get("kind")
    if kind not in ("prediction", "outcome"):
        raise ValueError("unsupported source event")
    state = raw.get("state") if kind == "prediction" else raw.get("final_state")
    if kind == "outcome" and raw.get("outcome") == "cancelled":
        state = "CANCELLED"
    return {"event_id": _ref(raw.get("event_id")), "case_ref": _ref(raw.get("case_id")),
            "at": at.astimezone(timezone.utc).timestamp(), "kind": kind,
            "state": state if isinstance(state, str) else "",
            "cpa_km": raw.get("projected_closest_km") if kind == "prediction" else raw.get("final_projected_closest_km"),
            "observed_km": raw.get("observed_closest_km") if kind == "outcome" else None,
            "coverage": "missing" if raw.get("coverage_missing") else "good"}


def collect(store: Store, *, start: int, end: int, records: Iterable[Mapping[str, object]],
            source_complete: bool, shadow: bool = True) -> WindowResult:
    """Checkpoint advances atomically with saved packets, never on partial reads."""
    if not shadow:
        raise ValueError("Phase 4 collector is shadow only")
    if end - start != 3600 or start % 3600 or end > time.time() + 60:
        raise ValueError("complete closed UTC hour required")
    if not source_complete:
        raise ValueError("incomplete evidence source cannot advance checkpoint")
    by_id: dict[str, dict] = {}
    overflow = False
    for raw in records:
        event = _event(raw, start, end)
        if event["event_id"] in by_id:
            if by_id[event["event_id"]] != event:
                raise ValueError("conflicting duplicate event")
            continue
        if len(by_id) >= MAX_EVENTS:
            overflow = True
            break
        by_id[event["event_id"]] = event
    if overflow:
        raise ValueError("hour exceeds bounded deterministic input; checkpoint unchanged")
    groups: dict[str, list[dict]] = defaultdict(list)
    for event in sorted(by_id.values(), key=lambda e: (e["at"], e["event_id"])):
        groups[event["case_ref"]].append(event)
    packets: list[tuple[str, str, str, str, str]] = []
    for case_ref, events in sorted(groups.items()):
        reasons = _anomalies(events)
        if not reasons:
            continue
        packet = {"schema": 1, "window_start": start, "case_ref": case_ref,
                  "evidence_refs": [e["event_id"] for e in events[:64]],
                  "event_count": len(events), "reasons": reasons,
                  "coverage": "missing" if any(e["coverage"] == "missing" for e in events)
                  else "partial" if any(e["coverage"] == "partial" for e in events) else "good",
                  "samples": [{"kind": e["kind"], "state": e["state"], "cpa_km": e["cpa_km"],
                               "observed_km": e["observed_km"]} for e in events[:16]]}
        content = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(content.encode()).hexdigest()
        packets.append((digest, case_ref, reasons[0], content, digest))
    if len(packets) > MAX_PACKETS:
        raise ValueError("hour exceeds bounded packet capacity; checkpoint unchanged")
    with store.transaction() as db:
        row = db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='collector_hour'").fetchone()
        if row is not None and int(row[0]) >= end:
            return WindowResult(0, len(by_id), False, int(row[0]))
        if row is not None and int(row[0]) != start:
            raise ValueError("non-contiguous audit window")
        for digest, case_ref, kind, content, checksum in packets:
            db.execute("""INSERT OR IGNORE INTO ai_ops_evidence
                (packet_id,window_start,case_ref,kind,packet_json,content_hash,created)
                VALUES(?,?,?,?,?,?,?)""", (digest, start, case_ref, kind, content, checksum, time.time()))
        db.execute("""INSERT INTO ai_ops_scheduler(name,checkpoint,updated) VALUES('collector_hour',?,?)
            ON CONFLICT(name) DO UPDATE SET checkpoint=excluded.checkpoint,updated=excluded.updated""", (str(end), time.time()))
        if packets:
            store._mark_dr_dirty(db)
    return WindowResult(len(packets), len(by_id), False, end)
