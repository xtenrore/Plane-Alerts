"""Phase 5 AI-assisted triage and finding lifecycle.

This module is control-plane analysis only. It consumes immutable sanitized
Phase-4 evidence packets and has no import or callback into flight-state code.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from .provider_adapters import AnalysisTask
from .provider_router import NoFreeRoute, Router
from .store import QueueFull, Store

CLASSIFICATIONS = frozenset({
    "NORMAL_EXPECTED_CHANGE", "EXPECTED_TURN", "STALE_INPUT", "PROVIDER_ISSUE",
    "COVERAGE_INCONCLUSIVE", "POSSIBLE_ESTIMATOR_ERROR", "ALERT_LIFECYCLE_ERROR",
    "ROUTE_GUARD_ANOMALY", "STORAGE_EVIDENCE_LOSS", "INSUFFICIENT_EVIDENCE", "INVESTIGATE",
})
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
SUBSYSTEMS = frozenset({"prediction", "alert_lifecycle", "route_guard", "provider", "coverage", "storage", "trajectory", "unknown"})
SUSPICIOUS = frozenset({"POSSIBLE_ESTIMATOR_ERROR", "ALERT_LIFECYCLE_ERROR", "ROUTE_GUARD_ANOMALY", "STORAGE_EVIDENCE_LOSS", "INVESTIGATE"})
TERMINAL_FINDINGS = frozenset({"RESOLVED", "EXPECTED_BEHAVIOR", "INCONCLUSIVE", "DUPLICATE", "ALREADY_FIXED", "FALSE_POSITIVE", "WONT_FIX_WITH_REASON"})
ACTIVE_FINDINGS = frozenset({"OPEN", "TRIAGED", "REVIEWING", "INVESTIGATING", "FIX_CANDIDATE", "DEPLOYED_PENDING_VERIFICATION"})
MAX_BATCH_CASES = 4
MAX_PROMPT_BYTES = 12_000
MAX_RESULT_TEXT = 1200
MAX_MALFORMED_BATCH_RETRIES = 3
_SECRET = re.compile(r"(?i)(authorization\s*:|bearer\s+|mongodb(?:\+srv)?://|gh[pousr]_|sk-[a-z0-9]|api[_-]?key|password\s*[:=]|secret\s*[:=])")
_SAFE_REF = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")


@dataclass(frozen=True)
class Validated:
    case_ref: str
    classification: str
    severity: str
    subsystem: str
    event_ids: tuple[str, ...]
    cpa_km: tuple[float, ...]
    observed_km: tuple[float, ...]
    states: tuple[str, ...]
    coverage: str
    rationale: str
    needs_review: bool
    validation_status: str = "VALID"
    validation_errors: tuple[str, ...] = ()

    def record(self) -> dict[str, object]:
        return {"case_ref": self.case_ref, "classification": self.classification, "severity": self.severity,
                "subsystem": self.subsystem, "event_ids": list(self.event_ids), "cpa_km": list(self.cpa_km),
                "observed_km": list(self.observed_km), "states": list(self.states), "coverage": self.coverage,
                "rationale": self.rationale, "needs_review": self.needs_review,
                "validation_status": self.validation_status, "validation_errors": list(self.validation_errors)}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe_text(value: object, *, limit: int = MAX_RESULT_TEXT) -> str:
    if not isinstance(value, str) or len(value) > limit or _SECRET.search(value):
        raise ValueError("unsafe or unbounded model text")
    return value


def _packet(store: Store, packet_id: str) -> dict[str, object]:
    row = store.db.execute("SELECT packet_json,content_hash FROM ai_ops_evidence WHERE packet_id=?", (packet_id,)).fetchone()
    if row is None:
        raise KeyError("evidence packet not found")
    content = row["packet_json"]
    digest = hashlib.sha256(content.encode()).hexdigest()
    if digest != packet_id or digest != row["content_hash"]:
        raise ValueError("evidence checksum mismatch")
    packet = json.loads(content)
    if not isinstance(packet, dict) or packet.get("schema") not in (1, 2):
        raise ValueError("unsupported sanitized evidence schema")
    return packet


def seed_pending_cases(store: Store) -> int:
    """Create one idempotent PENDING_AI row for each immutable evidence packet."""
    now = time.time()
    with store.transaction() as db:
        before = db.total_changes
        db.execute("""INSERT OR IGNORE INTO ai_ops_cases(packet_id,state,created,updated)
            SELECT packet_id,'PENDING_AI',?,? FROM ai_ops_evidence""", (now, now))
        added = db.total_changes - before
        if added:
            store._mark_dr_dirty(db)
    return added


def _queue_batch(store: Store, role: str, packet_ids: Sequence[str], *, priority: int) -> str:
    if role not in ("triage", "independent_review", "deep_investigation") or not packet_ids:
        raise ValueError("invalid Phase 5 batch")
    ids = tuple(packet_ids)
    base = hashlib.sha256((role + "\0" + "\0".join(ids)).encode()).hexdigest()[:16]
    payload = _json(list(ids))
    now = time.time()
    with store.transaction() as db:
        existing_rounds = db.execute("SELECT count(*) FROM ai_ops_batches WHERE role=? AND packet_ids_json=?", (role, payload)).fetchone()[0]
        batch_id = f"p5:{role[:3]}:{base}:{existing_rounds + 1}"
        pending = db.execute("SELECT count(*) FROM ai_ops_jobs WHERE status IN ('PENDING','RETRY','RUNNING')").fetchone()[0]
        if pending >= store.max_pending:
            raise QueueFull("Private Operations queue capacity reached")
        db.execute("INSERT INTO ai_ops_jobs(id,status,priority,created,updated) VALUES(?,'PENDING',?,?,?)", (batch_id, priority, now, now))
        db.execute("INSERT INTO ai_ops_batches(batch_id,role,packet_ids_json,status,created,updated) VALUES(?,?,?,'PENDING',?,?)",
                   (batch_id, role, payload, now, now))
        for packet_id in ids:
            result = db.execute("UPDATE ai_ops_cases SET batch_id=?,updated=? WHERE packet_id=? AND batch_id IS NULL", (batch_id, now, packet_id))
            if result.rowcount != 1:
                raise RuntimeError("case already assigned to another batch")
        store._mark_dr_dirty(db)
    return batch_id


def queue_triage_batches(store: Store, *, max_cases: int = MAX_BATCH_CASES, limit: int = 64) -> list[str]:
    if not 1 <= max_cases <= MAX_BATCH_CASES or not 1 <= limit <= 256:
        raise ValueError("bounded batch limits required")
    rows = store.db.execute("""SELECT packet_id FROM ai_ops_cases
        WHERE state='PENDING_AI' AND pending_role='triage' AND batch_id IS NULL ORDER BY created,packet_id LIMIT ?""", (limit,)).fetchall()
    queued: list[str] = []
    current_ids: list[str] = []
    current_packets: list[dict[str, object]] = []

    def flush() -> None:
        if current_ids:
            queued.append(_queue_batch(store, "triage", current_ids, priority=10))
            current_ids.clear()
            current_packets.clear()

    for row in rows:
        packet_id = row[0]
        packet = _packet(store, packet_id)
        candidate_packets = current_packets + [packet]
        fits = len(candidate_packets) <= max_cases
        if fits:
            try:
                _build_prompt("triage", candidate_packets)
            except ValueError:
                fits = False
        if not fits:
            flush()
            try:
                _build_prompt("triage", [packet])
            except ValueError:
                # A single sanitized packet should normally fit because Phase 4
                # is bounded. If it does not, preserve the evidence but close
                # the case deterministically instead of crash-looping forever.
                now = time.time()
                with store.transaction() as db:
                    db.execute("""UPDATE ai_ops_cases SET state='INCONCLUSIVE',pending_role='',classification='INSUFFICIENT_EVIDENCE',
                        severity='LOW',validation_status='REJECTED',last_error='local_context_bound',updated=? WHERE packet_id=?""",
                               (now, packet_id))
                    store._mark_dr_dirty(db)
                continue
        current_ids.append(packet_id)
        current_packets.append(packet)
    flush()
    return queued


def queue_review_batches(store: Store, *, limit: int = 64) -> list[str]:
    rows = store.db.execute("""SELECT packet_id,state,pending_role FROM ai_ops_cases
        WHERE batch_id IS NULL AND ((state='REVIEWING') OR (state='DEEP_REVIEW') OR (state='PENDING_AI' AND pending_role IN ('independent_review','deep_investigation'))) ORDER BY updated,packet_id LIMIT ?""", (limit,)).fetchall()
    return [_queue_batch(store, ('independent_review' if (r[1] == 'REVIEWING' or r[2] == 'independent_review') else 'deep_investigation'), [r[0]], priority=20 if (r[1] == 'REVIEWING' or r[2] == 'independent_review') else 30) for r in rows]


def _result_schema_instruction() -> str:
    return ("Return exactly one object in findings for every supplied case. Each object must contain exactly: "
            "case_ref, classification, severity, subsystem, event_ids, cpa_km, observed_km, states, coverage, rationale, needs_review. "
            "classification must be one of: " + ", ".join(sorted(CLASSIFICATIONS)) + ". "
            "severity: LOW|MEDIUM|HIGH|CRITICAL. subsystem: prediction|alert_lifecycle|route_guard|provider|coverage|storage|trajectory|unknown. "
            "Cite only event_ids and numeric/state/coverage values literally present in the evidence. Do not invent timestamps. "
            "Keep rationale qualitative: do not put numeric measurements, ETA/TTC, timestamps, altitude, speed, heading, destination, callsign, registration, or coordinates in rationale. "
            "Missing ADS-B coverage is inconclusive unless the deterministic packet itself proves a narrower infrastructure fact. "
            "Do not propose or perform flight-state changes.")


def _build_prompt(role: str, packets: Sequence[dict[str, object]], *, repair: bool = False) -> str:
    prefix = {"triage": "First-pass routine triage.", "independent_review": "Independent second review from original deterministic evidence only.",
              "deep_investigation": "Deep independent review of difficult evidence; do not copy another model conclusion."}[role]
    if repair:
        prefix += " Your previous structured shape was invalid; this is the single allowed repair attempt."
    prompt = prefix + "\n" + _result_schema_instruction() + "\nEVIDENCE=" + _json(list(packets))
    if len(prompt.encode()) > MAX_PROMPT_BYTES or _SECRET.search(prompt):
        raise ValueError("bounded sanitized prompt gate failed")
    return prompt


def _num_list(value: object) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("numeric claim list required")
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or abs(float(item)) > 100000:
            raise ValueError("invalid numeric claim")
        out.append(round(float(item), 3))
    return tuple(out)


def _str_list(value: object, *, limit: int = 64) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("bounded string list required")
    result = []
    for item in value:
        if not isinstance(item, str) or not _SAFE_REF.fullmatch(item):
            raise ValueError("invalid evidence reference")
        result.append(item)
    return tuple(result)


def _raw_validated(item: object) -> Validated:
    if not isinstance(item, dict) or set(item) != {"case_ref", "classification", "severity", "subsystem", "event_ids", "cpa_km",
                                                "observed_km", "states", "coverage", "rationale", "needs_review"}:
        raise ValueError("strict Phase 5 result schema mismatch")
    case_ref = item["case_ref"]
    if not isinstance(case_ref, str) or not _SAFE_REF.fullmatch(case_ref):
        raise ValueError("invalid case ref")
    classification = item["classification"]
    severity = item["severity"]
    subsystem = item["subsystem"]
    coverage = item["coverage"]
    if classification not in CLASSIFICATIONS or severity not in SEVERITIES or subsystem not in SUBSYSTEMS or coverage not in ("good", "partial", "missing"):
        raise ValueError("invalid structured enum")
    if not isinstance(item["needs_review"], bool):
        raise ValueError("needs_review must be boolean")
    return Validated(case_ref, classification, severity, subsystem, _str_list(item["event_ids"]), _num_list(item["cpa_km"]),
                     _num_list(item["observed_km"]), _str_list(item["states"], limit=32), coverage,
                     _safe_text(item["rationale"]), item["needs_review"])


def _classification_supported(packet: Mapping[str, object], result: Validated) -> bool:
    reasons = {str(v) for v in packet.get("reasons", []) if isinstance(v, str)} if isinstance(packet.get("reasons"), list) else set()
    coverage = packet.get("coverage")
    samples = packet.get("samples", []) if isinstance(packet.get("samples"), list) else []
    has_cpa = any(isinstance(v, dict) and isinstance(v.get("cpa_km"), (int, float)) and not isinstance(v.get("cpa_km"), bool) for v in samples)
    has_observed = any(isinstance(v, dict) and isinstance(v.get("observed_km"), (int, float)) and not isinstance(v.get("observed_km"), bool) for v in samples)
    c = result.classification
    if c == "STALE_INPUT":
        return any("stale" in reason for reason in reasons)
    if c == "PROVIDER_ISSUE":
        return "provider_changed" in reasons or any("provider" in reason for reason in reasons)
    if c == "COVERAGE_INCONCLUSIVE":
        return coverage in ("missing", "partial") or "missing_coverage" in reasons
    if c == "EXPECTED_TURN":
        return "trajectory_changed" in reasons or any("turn" in reason for reason in reasons)
    if c == "POSSIBLE_ESTIMATOR_ERROR":
        return coverage == "good" and has_cpa and (has_observed or "trajectory_changed" in reasons or "cancellation" in reasons)
    if c == "ALERT_LIFECYCLE_ERROR":
        return bool(reasons & {"cancellation", "requalification_after_cancellation", "qualify_cancel_oscillation"})
    if c == "ROUTE_GUARD_ANOMALY":
        return any("route" in reason or "airport" in reason for reason in reasons)
    if c == "STORAGE_EVIDENCE_LOSS":
        return any("storage" in reason or "evidence_loss" in reason for reason in reasons)
    return True


def _rationale_supported(rationale: str) -> bool:
    # All numerical claims have dedicated validated fields. Keeping free-form
    # rationale qualitative prevents an uncited number or unsupported flight
    # fact from bypassing deterministic evidence validation.
    if re.search(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", rationale):
        return False
    return re.search(r"\b(?:eta|ttc|timestamp|altitude|speed|heading|latitude|longitude|destination|callsign|registration)\b", rationale, re.I) is None


def validate_result(packet: Mapping[str, object], item: object) -> Validated:
    """Check every model citation/claim against deterministic packet contents."""
    try:
        result = _raw_validated(item)
    except ValueError as exc:
        case_ref = packet.get("case_ref") if isinstance(packet.get("case_ref"), str) else "unknown"
        return Validated(case_ref, "INSUFFICIENT_EVIDENCE", "LOW", "unknown", (), (), (), (),
                         packet.get("coverage") if packet.get("coverage") in ("good", "partial", "missing") else "missing",
                         "Structured result rejected by deterministic validation.", False, "REJECTED", (str(exc),))
    errors = []
    refs = set(packet.get("evidence_refs", [])) if isinstance(packet.get("evidence_refs"), list) else set()
    samples = packet.get("samples", []) if isinstance(packet.get("samples"), list) else []
    if result.case_ref != packet.get("case_ref"):
        errors.append("case_ref")
    if any(event_id not in refs for event_id in result.event_ids):
        errors.append("event_id")
    allowed_cpa = {round(float(s["cpa_km"]), 3) for s in samples if isinstance(s, dict) and isinstance(s.get("cpa_km"), (int, float)) and not isinstance(s.get("cpa_km"), bool)}
    allowed_obs = {round(float(s["observed_km"]), 3) for s in samples if isinstance(s, dict) and isinstance(s.get("observed_km"), (int, float)) and not isinstance(s.get("observed_km"), bool)}
    allowed_states = {str(s.get("state")) for s in samples if isinstance(s, dict) and s.get("state")}
    if any(v not in allowed_cpa for v in result.cpa_km):
        errors.append("cpa_km")
    if any(v not in allowed_obs for v in result.observed_km):
        errors.append("observed_km")
    if any(v not in allowed_states for v in result.states):
        errors.append("state")
    packet_coverage = packet.get("coverage")
    if result.coverage != packet_coverage:
        errors.append("coverage")
    if not _rationale_supported(result.rationale):
        errors.append("unsupported_rationale")
    if errors:
        return replace(result, classification="INSUFFICIENT_EVIDENCE", severity="LOW", subsystem="unknown",
                       needs_review=False, validation_status="REJECTED", validation_errors=tuple(sorted(set(errors))),
                       rationale="Model claims rejected because they do not match deterministic evidence.")
    if packet_coverage == "missing" and result.classification not in ("COVERAGE_INCONCLUSIVE", "INSUFFICIENT_EVIDENCE", "PROVIDER_ISSUE", "STORAGE_EVIDENCE_LOSS"):
        return replace(result, classification="COVERAGE_INCONCLUSIVE", severity="LOW", subsystem="coverage", needs_review=False,
                       validation_status="DOWNGRADED", validation_errors=("missing_coverage",),
                       rationale="Missing ADS-B coverage prevents a stronger prediction conclusion.")
    if not _classification_supported(packet, result):
        return replace(result, classification="INSUFFICIENT_EVIDENCE", severity="LOW", subsystem="unknown",
                       needs_review=False, validation_status="REJECTED", validation_errors=("classification_evidence",),
                       rationale="Model classification rejected because the deterministic packet does not contain its required evidence class.")
    return result


def _strict_batch(packet_map: Mapping[str, Mapping[str, object]], analysis: object) -> tuple[list[Validated], bool]:
    if not isinstance(analysis, dict) or set(analysis) != {"summary", "findings"} or not isinstance(analysis["summary"], str) or len(analysis["summary"]) > 4000 or not isinstance(analysis["findings"], list):
        return [], False
    if len(analysis["findings"]) != len(packet_map):
        return [], False
    by_case: dict[str, Validated] = {}
    for item in analysis["findings"]:
        if not isinstance(item, dict) or not isinstance(item.get("case_ref"), str) or item["case_ref"] not in packet_map or item["case_ref"] in by_case:
            return [], False
        by_case[item["case_ref"]] = validate_result(packet_map[item["case_ref"]], item)
    return [by_case[k] for k in packet_map], len(by_case) == len(packet_map)


def _first_provider(store: Store, packet_id: str) -> str | None:
    row = store.db.execute("SELECT provider FROM ai_ops_reviews WHERE packet_id=? AND role='triage' ORDER BY created LIMIT 1", (packet_id,)).fetchone()
    return row[0] if row else None


def _persist_review(store: Store, packet_id: str, role: str, provider: str, model: str, slot: str,
                    result: Validated, *, agreement: str | None = None, disposition: str | None = None) -> None:
    rid = hashlib.sha256((packet_id + "\0" + role + "\0" + provider + "\0" + slot + "\0" + Store.digest(result.record())).encode()).hexdigest()
    payload = _json(result.record())
    if len(payload.encode()) > 8192 or _SECRET.search(payload):
        raise ValueError("review persistence privacy gate failed")
    with store.transaction() as db:
        db.execute("""INSERT OR IGNORE INTO ai_ops_reviews(review_id,packet_id,role,provider,model,key_slot,
            classification,severity,validation_status,result_json,agreement,disposition,created)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, packet_id, role, provider, model, slot, result.classification, result.severity, result.validation_status,
             payload, agreement, disposition, time.time()))
        store._mark_dr_dirty(db)


def _signature(packet: Mapping[str, object], result: Validated) -> str:
    primary_reason = str((packet.get("reasons") or ["unknown"])[0])[:80]
    raw = {"classification": result.classification, "case_ref": packet.get("case_ref"), "pattern": primary_reason, "subsystem": result.subsystem}
    return hashlib.sha256(_json(raw).encode()).hexdigest()


def upsert_finding(store: Store, packet_id: str, result: Validated) -> tuple[str | None, str]:
    packet = _packet(store, packet_id)
    sig = _signature(packet, result)
    now = time.time()
    with store.transaction() as db:
        prior = db.execute("SELECT * FROM ai_ops_findings WHERE signature=? ORDER BY occurrence DESC LIMIT 1", (sig,)).fetchone()
        if prior is not None and prior["status"] in ACTIVE_FINDINGS:
            db.execute("INSERT OR IGNORE INTO ai_ops_finding_packets(finding_id,packet_id,created) VALUES(?,?,?)", (prior["finding_id"], packet_id, now))
            db.execute("UPDATE ai_ops_findings SET updated=? WHERE finding_id=?", (now, prior["finding_id"]))
            store._mark_dr_dirty(db)
            return prior["finding_id"], "DEDUPED_ACTIVE"
        if prior is not None and prior["status"] != "RESOLVED":
            # Repeated expected/inconclusive/false-positive evidence links to history,
            # rather than growing another active finding.
            db.execute("INSERT OR IGNORE INTO ai_ops_finding_packets(finding_id,packet_id,created) VALUES(?,?,?)", (prior["finding_id"], packet_id, now))
            store._mark_dr_dirty(db)
            return prior["finding_id"], "DUPLICATE_TERMINAL"
        occurrence = 1 if prior is None else int(prior["occurrence"]) + 1
        finding_id = f"F-{sig[:12]}-{occurrence}"
        db.execute("""INSERT INTO ai_ops_findings(finding_id,signature,occurrence,status,classification,severity,subsystem,case_ref,
            previous_finding_id,created,updated) VALUES(?,?,?,'TRIAGED',?,?,?,?,?,?,?)""",
            (finding_id, sig, occurrence, result.classification, result.severity, result.subsystem, result.case_ref,
             prior["finding_id"] if prior is not None else None, now, now))
        db.execute("INSERT INTO ai_ops_finding_packets(finding_id,packet_id,created) VALUES(?,?,?)", (finding_id, packet_id, now))
        store._mark_dr_dirty(db)
        return finding_id, "REGRESSION_OCCURRENCE" if prior is not None else "CREATED"


def _transition_allowed(old: str, new: str) -> bool:
    allowed = {
        "OPEN": {"TRIAGED", *TERMINAL_FINDINGS},
        "TRIAGED": {"REVIEWING", "INVESTIGATING", *TERMINAL_FINDINGS},
        "REVIEWING": {"INVESTIGATING", *TERMINAL_FINDINGS},
        "INVESTIGATING": {"FIX_CANDIDATE", *TERMINAL_FINDINGS},
        "FIX_CANDIDATE": {"DEPLOYED_PENDING_VERIFICATION", "RESOLVED", "WONT_FIX_WITH_REASON"},
        "DEPLOYED_PENDING_VERIFICATION": {"RESOLVED", "INVESTIGATING"},
    }
    return new in allowed.get(old, set())


def transition_finding(store: Store, finding_id: str, new_status: str) -> None:
    if new_status not in ACTIVE_FINDINGS | TERMINAL_FINDINGS:
        raise ValueError("unknown finding status")
    now = time.time()
    with store.transaction() as db:
        row = db.execute("SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None:
            raise KeyError("finding not found")
        if row[0] == new_status:
            return
        if not _transition_allowed(row[0], new_status):
            raise ValueError("invalid finding lifecycle transition")
        db.execute("UPDATE ai_ops_findings SET status=?,updated=? WHERE finding_id=?", (new_status, now, finding_id))
        store._mark_dr_dirty(db)


def _cleanup_terminal(store: Store, db, finding_id: str, terminal_state: str, now: float) -> None:
    batch_ids = [r[0] for r in db.execute("SELECT DISTINCT batch_id FROM ai_ops_cases WHERE finding_id=? AND batch_id IS NOT NULL", (finding_id,))]
    db.execute("UPDATE ai_ops_cases SET state=?,batch_id=NULL,updated=? WHERE finding_id=?", (terminal_state, now, finding_id))
    for batch_id in batch_ids:
        db.execute("UPDATE ai_ops_steps SET status=CASE WHEN status='RUNNING' THEN 'FAILED' ELSE status END,lease_owner=NULL,lease_until=NULL WHERE job_id=?", (batch_id,))
        db.execute("UPDATE ai_ops_attempts SET ended=coalesce(ended,?),result=coalesce(result,'TERMINAL') WHERE job_id=?", (now, batch_id))
        db.execute("UPDATE ai_ops_jobs SET status=CASE WHEN status='COMPLETE' THEN status ELSE 'FAILED' END,lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?", (now, batch_id))
        db.execute("UPDATE ai_ops_batches SET status='TERMINAL',updated=? WHERE batch_id=?", (now, batch_id))


def terminal_finding(store: Store, finding_id: str, status: str, *, reason: str) -> None:
    if status not in TERMINAL_FINDINGS - {"RESOLVED"} or not reason or len(reason) > 500 or _SECRET.search(reason):
        raise ValueError("terminal reason required")
    now = time.time()
    with store.transaction() as db:
        row = db.execute("SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None or (row[0] != status and not _transition_allowed(row[0], status)):
            raise ValueError("invalid terminal transition")
        db.execute("UPDATE ai_ops_findings SET status=?,resolution_json=?,updated=? WHERE finding_id=?", (status, _json({"reason": reason}), now, finding_id))
        _cleanup_terminal(store, db, finding_id, status, now)
        store._mark_dr_dirty(db)


def resolve_finding(store: Store, finding_id: str, *, fix_commit: str, fix_version: str,
                    tests_passed: bool, ci_passed: bool, deployment_required: bool, deployed: bool,
                    production_verified: bool, replay_summary: str, error_museum_ref: str = "", user_feedback: str = "") -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", fix_commit) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", fix_version):
        raise ValueError("exact fix commit/version required")
    if not tests_passed or not ci_passed or (deployment_required and not deployed) or not production_verified:
        raise ValueError("verified resolution gates not satisfied")
    for text in (replay_summary, error_museum_ref, user_feedback):
        if len(text) > 500 or _SECRET.search(text):
            raise ValueError("unsafe resolution metadata")
    now = time.time()
    resolution = {"tests_passed": True, "ci_passed": True, "deployment_required": deployment_required,
                  "deployed": deployed, "production_verified": True, "replay_summary": replay_summary,
                  "error_museum_ref": error_museum_ref, "user_feedback": user_feedback, "verified_at": int(now)}
    with store.transaction() as db:
        row = db.execute("SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None or row[0] in TERMINAL_FINDINGS - {"RESOLVED"}:
            raise ValueError("finding cannot be resolved")
        db.execute("UPDATE ai_ops_findings SET status='RESOLVED',fix_commit=?,fix_version=?,resolution_json=?,updated=? WHERE finding_id=?",
                   (fix_commit, fix_version, _json(resolution), now, finding_id))
        _cleanup_terminal(store, db, finding_id, "RESOLVED", now)
        store._mark_dr_dirty(db)


def build_handoff(store: Store, *, recent_resolved_limit: int = 5) -> dict[str, object]:
    if not 0 <= recent_resolved_limit <= 20:
        raise ValueError("bounded handoff history required")
    unresolved = [dict(r) for r in store.db.execute("""SELECT finding_id,status,classification,severity,subsystem,case_ref,
        previous_finding_id,created,updated FROM ai_ops_findings WHERE status NOT IN
        ('RESOLVED','EXPECTED_BEHAVIOR','INCONCLUSIVE','DUPLICATE','ALREADY_FIXED','FALSE_POSITIVE','WONT_FIX_WITH_REASON')
        ORDER BY severity DESC,updated""")]
    recent = [dict(r) for r in store.db.execute("""SELECT finding_id,status,classification,fix_commit,fix_version,updated
        FROM ai_ops_findings WHERE status IN ('RESOLVED','EXPECTED_BEHAVIOR','INCONCLUSIVE','DUPLICATE','ALREADY_FIXED','FALSE_POSITIVE','WONT_FIX_WITH_REASON')
        ORDER BY updated DESC LIMIT ?""", (recent_resolved_limit,))]
    return {"schema": 1, "unresolved_findings": unresolved, "recent_resolved": recent}


def _mark_case(store: Store, packet_id: str, *, state: str, result: Validated, finding_id: str | None, batch_id: str | None,
               pending_role: str = '', error: str | None = None) -> None:
    with store.transaction() as db:
        db.execute("""UPDATE ai_ops_cases SET state=?,pending_role=?,classification=?,severity=?,validation_status=?,finding_id=?,batch_id=?,
            last_error=?,updated=? WHERE packet_id=?""", (state, pending_role, result.classification, result.severity, result.validation_status,
            finding_id, batch_id, error, time.time(), packet_id))
        store._mark_dr_dirty(db)


def _fail_batch(store: Store, batch_id: str, packet_ids: Sequence[str], worker: str, *, reason: str, complete_job: bool, role: str = 'triage') -> None:
    now = time.time()
    with store.transaction() as db:
        for pid in packet_ids:
            row = db.execute("SELECT retry_count FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()
            retries = int(row[0]) + 1 if row else 1
            terminal_malformed = complete_job and reason == "malformed_after_one_repair" and retries >= MAX_MALFORMED_BATCH_RETRIES
            if terminal_malformed:
                db.execute("""UPDATE ai_ops_cases SET state='INCONCLUSIVE',pending_role='',batch_id=NULL,retry_count=?,last_error=?,
                    classification='INSUFFICIENT_EVIDENCE',severity='LOW',validation_status='REJECTED',updated=? WHERE packet_id=?""",
                    (retries, reason[:80], now, pid))
            elif complete_job:
                db.execute("UPDATE ai_ops_cases SET state='PENDING_AI',batch_id=NULL,retry_count=?,last_error=?,updated=? WHERE packet_id=?",
                           (retries, reason[:80], now, pid))
            else:
                # Keep the existing batch assignment so the next scheduler
                # iteration resumes this exact durable job instead of creating a
                # duplicate batch for the same case.
                db.execute("UPDATE ai_ops_cases SET state='PENDING_AI',retry_count=?,last_error=?,updated=? WHERE packet_id=?",
                           (retries, reason[:80], now, pid))
        db.execute("UPDATE ai_ops_batches SET status=?,updated=? WHERE batch_id=?", ("FAILED" if complete_job else "PENDING_AI", now, batch_id))
        store._mark_dr_dirty(db)
    if complete_job:
        # Add a durable terminal disposition step so every started step is complete.
        if store.begin_step(batch_id, "batch_disposition", worker, {"reason": reason}):
            store.complete_step(batch_id, "batch_disposition", worker, {"status": "FAILED", "reason": reason})
        # Any current model-call step was already completed when complete_job=True.
        store.finish_job(batch_id, worker)
    else:
        store.retry_job(batch_id, worker, result="PENDING_AI")


def _analysis_call(store: Store, router: Router, models: Mapping[str, str], *, batch_id: str, role: str,
                   packet_ids: Sequence[str], packets: Sequence[dict[str, object]], worker: str,
                   preferred: Sequence[str], exclude: Sequence[str], repair: bool, now: float | None) -> tuple[object, str, str, str] | None:
    step = "repair_call" if repair else "model_call"
    marker = {"role": role, "packet_ids": list(packet_ids), "repair": repair}
    cached = store.step_output(batch_id, step)
    if cached is not None:
        if not isinstance(cached, dict) or set(cached) != {"analysis", "provider", "model", "slot"}:
            raise ValueError("invalid cached model-call checkpoint")
        return cached["analysis"], cached["provider"], cached["model"], cached["slot"]
    if not store.begin_step(batch_id, step, worker, marker):
        cached = store.step_output(batch_id, step)
        if cached is None:
            raise RuntimeError("completed call checkpoint missing output")
        return cached["analysis"], cached["provider"], cached["model"], cached["slot"]
    prompt = _build_prompt(role, packets, repair=repair)
    try:
        result = router.execute(AnalysisTask(role, prompt, batch_id=batch_id, packet_id=packet_ids[0] if len(packet_ids) == 1 else ""), models,
                                now=now, preferred_providers=preferred, exclude_providers=exclude, max_attempts=20)
    except NoFreeRoute:
        return None
    checkpoint = {"analysis": result.analysis, "provider": result.provider, "model": result.model, "slot": result.slot_name}
    store.complete_step(batch_id, step, worker, checkpoint)
    return result.analysis, result.provider, result.model, result.slot_name


def process_next(store: Store, router: Router, models: Mapping[str, str], *, worker: str = "phase5-worker", now: float | None = None) -> str | None:
    """Process one durable batch. Never performs scheduling or flight-state work."""
    batch_id = store.claim(worker, lease_seconds=300)
    if batch_id is None:
        return None
    batch = store.db.execute("SELECT role,packet_ids_json FROM ai_ops_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        store.retry_job(batch_id, worker, result="UNKNOWN_JOB")
        return "UNKNOWN_JOB"
    role = batch["role"]
    packet_ids = tuple(json.loads(batch["packet_ids_json"]))
    packets = [_packet(store, pid) for pid in packet_ids]
    packet_map = {str(p["case_ref"]): p for p in packets}
    if len(packet_map) != len(packets):
        _fail_batch(store, batch_id, packet_ids, worker, reason="duplicate_case_ref", complete_job=False, role=role)
        return "PENDING_AI"
    exclude: list[str] = []
    preferred = ["groq", "mistral", "gemini", "openrouter"]
    if role == "independent_review":
        first = _first_provider(store, packet_ids[0])
        exclude = [first] if first else []
        preferred = ["mistral", "gemini", "groq", "openrouter"]
    elif role == "deep_investigation":
        preferred = ["gemini", "mistral", "groq", "openrouter"]

    call = _analysis_call(store, router, models, batch_id=batch_id, role=role, packet_ids=packet_ids, packets=packets,
                          worker=worker, preferred=preferred, exclude=exclude, repair=False, now=now)
    if call is None:
        _fail_batch(store, batch_id, packet_ids, worker, reason="capacity_unavailable", complete_job=False, role=role)
        return "PENDING_AI"
    analysis, provider, model, slot = call
    results, valid_shape = _strict_batch(packet_map, analysis)
    if not valid_shape:
        repair_call = _analysis_call(store, router, models, batch_id=batch_id, role=role, packet_ids=packet_ids, packets=packets,
                                     worker=worker, preferred=preferred, exclude=exclude, repair=True, now=now)
        if repair_call is None:
            _fail_batch(store, batch_id, packet_ids, worker, reason="repair_capacity_unavailable", complete_job=False, role=role)
            return "PENDING_AI"
        analysis, provider, model, slot = repair_call
        results, valid_shape = _strict_batch(packet_map, analysis)
        if not valid_shape:
            _fail_batch(store, batch_id, packet_ids, worker, reason="malformed_after_one_repair", complete_job=True, role=role)
            return "PENDING_AI"

    for packet_id, packet, result in zip(packet_ids, packets, results):
        # Per-case commit steps make a crash in the middle of a batch resumable.
        step_id = "case:" + packet_id[:48]
        marker = {"packet_id": packet_id, "role": role, "result": result.record()}
        if not store.begin_step(batch_id, step_id, worker, marker):
            continue
        agreement = None
        finding_id: str | None = None
        next_state = "TRIAGED"
        if role == "triage":
            important = result.classification in SUSPICIOUS or result.severity in ("HIGH", "CRITICAL") or result.needs_review
            if important and result.validation_status != "REJECTED":
                finding_id, _ = upsert_finding(store, packet_id, result)
                if finding_id:
                    try:
                        transition_finding(store, finding_id, "REVIEWING")
                    except ValueError:
                        pass
                next_state = "REVIEWING"
            _persist_review(store, packet_id, role, provider, model, slot, result)
        elif role == "independent_review":
            first = store.db.execute("SELECT classification FROM ai_ops_reviews WHERE packet_id=? AND role='triage' ORDER BY created LIMIT 1", (packet_id,)).fetchone()
            finding = store.db.execute("SELECT finding_id FROM ai_ops_cases WHERE packet_id=?", (packet_id,)).fetchone()
            finding_id = finding[0] if finding else None
            agreement = "AGREE" if first and first[0] == result.classification else "DISAGREE"
            _persist_review(store, packet_id, role, provider, model, slot, result, agreement=agreement)
            if finding_id:
                try: transition_finding(store, finding_id, "INVESTIGATING")
                except ValueError: pass
            next_state = "DEEP_REVIEW" if agreement == "DISAGREE" or result.severity in ("HIGH", "CRITICAL") else "INVESTIGATING"
        else:
            finding = store.db.execute("SELECT finding_id FROM ai_ops_cases WHERE packet_id=?", (packet_id,)).fetchone()
            finding_id = finding[0] if finding else None
            previous = [r[0] for r in store.db.execute("SELECT classification FROM ai_ops_reviews WHERE packet_id=? AND role IN ('triage','independent_review') ORDER BY created", (packet_id,))]
            agreement = "SUPPORTS_PRIOR" if result.classification in previous else "DISAGREEMENT_REMAINS"
            _persist_review(store, packet_id, role, provider, model, slot, result, agreement=agreement)
            if finding_id:
                with store.transaction() as db:
                    db.execute("UPDATE ai_ops_findings SET classification=?,severity=?,subsystem=?,status='INVESTIGATING',updated=? WHERE finding_id=?",
                               (result.classification, result.severity, result.subsystem, time.time(), finding_id))
                    store._mark_dr_dirty(db)
            next_state = "INVESTIGATING"
        _mark_case(store, packet_id, state=next_state, result=result, finding_id=finding_id, batch_id=None, pending_role=('independent_review' if next_state == 'REVIEWING' else 'deep_investigation' if next_state == 'DEEP_REVIEW' else ''))
        store.complete_step(batch_id, step_id, worker, {"state": next_state, "classification": result.classification,
                                                       "validation": result.validation_status, "agreement": agreement})

    with store.transaction() as db:
        db.execute("UPDATE ai_ops_batches SET status='COMPLETE',updated=? WHERE batch_id=?", (time.time(), batch_id))
        store._mark_dr_dirty(db)
    store.finish_job(batch_id, worker)
    return "COMPLETE"


def process_backlog(store: Store, router: Router, models: Mapping[str, str], *, worker: str = "phase5-worker", max_batches: int = 64) -> dict[str, int]:
    """Bounded orchestration iteration; callers decide when a later iteration runs."""
    if not 1 <= max_batches <= 256:
        raise ValueError("bounded backlog iteration required")
    seed_pending_cases(store)
    queue_triage_batches(store)
    counts = {"complete": 0, "pending_ai": 0}
    for _ in range(max_batches):
        outcome = process_next(store, router, models, worker=worker)
        if outcome is None:
            queue_review_batches(store)
            outcome = process_next(store, router, models, worker=worker)
            if outcome is None:
                break
        if outcome == "COMPLETE":
            counts["complete"] += 1
        if outcome == "PENDING_AI":
            counts["pending_ai"] += 1
            # Never spin on unavailable capacity or malformed output within the
            # same scheduler iteration. Durable state is resumed later.
            break
        queue_review_batches(store)
    return counts
