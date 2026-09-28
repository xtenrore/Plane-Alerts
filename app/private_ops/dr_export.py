"""Sanitized portable checkpoint for the isolated Private Operations service.

The GitHub destination must be a dedicated private repository. The checkpoint
preserves durable orchestration and Phase-5 canonical lifecycle state, while
omitting prompts, raw provider bodies, credentials, locations and verbose model
text. The live persistent volume remains authoritative between checkpoints.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .store import Store

_SAFE_ID = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 _:./-]{0,200}$")
_SECRET = re.compile(r"(?i)(authorization|bearer\s|mongodb(?:\+srv)?://|mongo[_-]?uri|telegram|railway[_-]?token|gh[pousr]_|sk-[a-z0-9]|eyJ[A-Za-z0-9_-]{20}|api[_-]?key|token|password|secret|\b(?:lat|lon|latitude|longitude)\b)")


def _safe(value: object) -> object:
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("too many export fields")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key) or _SECRET.search(key):
                raise ValueError("unsafe export key")
            result[key] = _safe(item)
        return result
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError("too many export items")
        return [_safe(item) for item in value]
    if isinstance(value, str):
        if not _SAFE_TEXT.fullmatch(value) or _SECRET.search(value):
            raise ValueError("unsafe export text")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and 0 <= value <= 4_000_000_000:
        return value
    if isinstance(value, float) and math.isfinite(value) and -100000 <= value <= 100000:
        return value
    raise ValueError("unsupported or private export value")


def _id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or _SECRET.search(value):
        raise ValueError("unsafe checkpoint identifier")
    return value


def _bounded_note(value: object, *, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or _SECRET.search(value) or any(ord(ch) < 32 and ch not in "\t\n\r" for ch in value):
        raise ValueError("unsafe checkpoint note")
    return value


def _review_compact(text: str) -> dict[str, object]:
    """Preserve the validated conclusion, never a raw provider response/prompt."""
    value = json.loads(text)
    expected = {"case_ref", "classification", "severity", "subsystem", "event_ids", "cpa_km", "observed_km",
                "states", "coverage", "rationale", "needs_review", "validation_status", "validation_errors"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("invalid review result")
    def refs(name: str, limit: int = 64) -> list[str]:
        items = value[name]
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("invalid review list")
        return [_id(v) for v in items]
    def nums(name: str) -> list[float]:
        items = value[name]
        if not isinstance(items, list) or len(items) > 32:
            raise ValueError("invalid review numeric list")
        out = []
        for item in items:
            if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or abs(float(item)) > 100000:
                raise ValueError("invalid review metric")
            out.append(float(item))
        return out
    rationale = value["rationale"]
    try:
        rationale = _bounded_note(rationale, limit=1200)
    except ValueError:
        # Preserve the deterministic conclusion even if the explanatory prose
        # trips the stricter DR privacy gate.
        rationale = "Review rationale omitted by DR privacy gate."
    return {
        "case_ref": _id(value["case_ref"]), "classification": _id(value["classification"]),
        "severity": _id(value["severity"]), "subsystem": _id(value["subsystem"]),
        "event_ids": refs("event_ids"), "cpa_km": nums("cpa_km"), "observed_km": nums("observed_km"),
        "states": refs("states", 32), "coverage": _id(value["coverage"]), "rationale": rationale,
        "needs_review": bool(value["needs_review"]), "validation_status": _id(value["validation_status"]),
        "validation_errors": refs("validation_errors", 32),
    }


def _resolution_compact(text: str | None) -> dict[str, object] | None:
    if not text:
        return None
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("invalid resolution record")
    if set(value) == {"reason"}:
        try:
            reason = _bounded_note(value["reason"], limit=500)
        except ValueError:
            reason = "Terminal reason omitted by DR privacy gate."
        return {"reason": reason}
    allowed = {"tests_passed", "ci_passed", "deployment_required", "deployed", "production_verified",
               "replay_summary", "error_museum_ref", "user_feedback", "verified_at"}
    if set(value) != allowed:
        raise ValueError("invalid verified resolution record")
    record: dict[str, object] = {}
    for key in ("tests_passed", "ci_passed", "deployment_required", "deployed", "production_verified"):
        if not isinstance(value[key], bool):
            raise ValueError("invalid resolution gate")
        record[key] = value[key]
    if not isinstance(value["verified_at"], int) or not 0 <= value["verified_at"] <= 4_000_000_000:
        raise ValueError("invalid resolution timestamp")
    record["verified_at"] = value["verified_at"]
    for key in ("replay_summary", "error_museum_ref", "user_feedback"):
        try:
            record[key] = _bounded_note(value[key], limit=500)
        except ValueError:
            record[key] = key + " omitted by DR privacy gate."
    return record

def export(store: Store, *, source_commit: str) -> tuple[bytes, dict[str, object]]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("exact source commit required")
    if store.health()["integrity"] != "ok":
        raise RuntimeError("unhealthy database")
    jobs = []
    for job in store.db.execute("SELECT id,status,priority,created,updated FROM ai_ops_jobs ORDER BY id"):
        status = "RETRY" if job[1] == "RUNNING" else job[1]
        jobs.append({"id": _id(job[0]), "status": status, "priority": job[2], "created": job[3], "updated": job[4]})
    steps = []
    unfinished_phase5 = {row[0] for row in store.db.execute(
        "SELECT id FROM ai_ops_jobs WHERE id LIKE 'p5:%' AND status!='COMPLETE'")}
    for step in store.db.execute("SELECT job_id,step_id,status,input_hash,output_hash,attempts FROM ai_ops_steps ORDER BY job_id,step_id"):
        status = "RETRY" if step[2] == "RUNNING" else step[2]
        # Sanitized DR intentionally omits model-call output_json. If disaster
        # recovery catches an unfinished Phase-5 batch after its call checkpoint,
        # retry that bounded call; already committed per-case steps remain COMPLETE.
        if step[0] in unfinished_phase5 and step[1] in ("model_call", "repair_call") and status == "COMPLETE":
            status = "RETRY"
        steps.append({"job_id": _id(step[0]), "step_id": _id(step[1]), "status": status,
                      "input_hash": step[3], "output_hash": step[4], "attempts": step[5]})
    checkpoints = [{"name": _id(row[0]), "checkpoint": _id(row[1])}
                   for row in store.db.execute("SELECT name,checkpoint FROM ai_ops_scheduler ORDER BY name")]
    evidence = []
    for row in store.db.execute("SELECT packet_id,window_start,case_ref,kind,packet_json,content_hash FROM ai_ops_evidence ORDER BY packet_id"):
        packet = _safe(json.loads(row[4]))
        content = json.dumps(packet, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(content.encode()).hexdigest() != row[0] or row[0] != row[5]:
            raise ValueError("evidence packet checksum mismatch")
        evidence.append({"packet_id": row[0], "window_start": row[1], "case_ref": _id(row[2]),
                         "kind": _id(row[3]), "packet": packet})

    batches = []
    cases = []
    reviews = []
    findings = []
    finding_packets = []
    if store.db.execute("PRAGMA user_version").fetchone()[0] >= 5:
        for row in store.db.execute("SELECT batch_id,role,packet_ids_json,status,created,updated FROM ai_ops_batches ORDER BY batch_id"):
            packet_ids = json.loads(row[2])
            if not isinstance(packet_ids, list) or len(packet_ids) > 64:
                raise ValueError("invalid batch packet list")
            batches.append({"batch_id": _id(row[0]), "role": _id(row[1]), "packet_ids": [_id(v) for v in packet_ids],
                            "status": _id(row[3]), "created": row[4], "updated": row[5]})
        for row in store.db.execute("""SELECT packet_id,state,pending_role,classification,severity,validation_status,finding_id,
            batch_id,retry_count,last_error,created,updated,escalation_reason FROM ai_ops_cases ORDER BY packet_id"""):
            cases.append({"packet_id": _id(row[0]), "state": _id(row[1]), "pending_role": _id(row[2]) if row[2] else "",
                          "classification": _id(row[3]) if row[3] else None, "severity": _id(row[4]) if row[4] else None,
                          "validation_status": _id(row[5]) if row[5] else None, "finding_id": _id(row[6]) if row[6] else None,
                          "batch_id": _id(row[7]) if row[7] else None, "retry_count": row[8],
                          "last_error": _id(row[9]) if row[9] else None, "created": row[10], "updated": row[11],
                          "escalation_reason": _id(row[12]) if row[12] else ""})
        for row in store.db.execute("""SELECT review_id,packet_id,role,provider,model,key_slot,classification,severity,
            validation_status,result_json,agreement,disposition,created,independence FROM ai_ops_reviews ORDER BY review_id"""):
            compact = _review_compact(row[9])
            reviews.append({"review_id": _id(row[0]), "packet_id": _id(row[1]), "role": _id(row[2]), "provider": _id(row[3]),
                            "model": _id(row[4]), "key_slot": _id(row[5]), "classification": _id(row[6]), "severity": _id(row[7]),
                            "validation_status": _id(row[8]), "result": compact, "agreement": _id(row[10]) if row[10] else None,
                            "disposition": _id(row[11]) if row[11] else None, "created": row[12], "independence": _id(row[13])})
        for row in store.db.execute("""SELECT finding_id,signature,occurrence,status,classification,severity,subsystem,case_ref,
            previous_finding_id,fix_commit,fix_version,resolution_json,created,updated FROM ai_ops_findings ORDER BY signature,occurrence"""):
            findings.append({"finding_id": _id(row[0]), "signature": row[1], "occurrence": row[2], "status": _id(row[3]),
                             "classification": _id(row[4]), "severity": _id(row[5]), "subsystem": _id(row[6]), "case_ref": _id(row[7]),
                             "previous_finding_id": _id(row[8]) if row[8] else None, "fix_commit": row[9], "fix_version": row[10],
                             "resolution": _resolution_compact(row[11]), "created": row[12], "updated": row[13]})
        for row in store.db.execute("SELECT finding_id,packet_id,created FROM ai_ops_finding_packets ORDER BY finding_id,packet_id"):
            finding_packets.append({"finding_id": _id(row[0]), "packet_id": _id(row[1]), "created": row[2]})

    feedback = [{"finding_id": _id(row[0]), "verdict": _id(row[1]), "updated": row[2]}
                for row in store.db.execute("SELECT finding_id,verdict,updated FROM ai_ops_feedback ORDER BY finding_id")]
    record = {"schema": 4, "source_commit": source_commit, "jobs": jobs, "steps": steps,
              "checkpoints": checkpoints, "evidence": evidence, "batches": batches, "cases": cases,
              "reviews": reviews, "findings": findings, "finding_packets": finding_packets, "feedback": feedback}
    data = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 2_000_000 or _SECRET.search(data.decode()):
        raise ValueError("sanitized export failed privacy gate")
    manifest = {"schema": 4, "source_commit": source_commit, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "jobs": len(jobs), "steps": len(steps), "evidence": len(evidence), "batches": len(batches), "cases": len(cases),
                "reviews": len(reviews), "findings": len(findings), "finding_packets": len(finding_packets), "feedback": len(feedback)}
    return data, manifest


def restore_empty(directory: str | Path, data: bytes, manifest: dict[str, object]) -> Store:
    """Restore into an empty dedicated directory only; never overwrite live state."""
    root = Path(directory)
    if not root.is_dir() or any(root.iterdir()):
        raise FileExistsError("restore target must be an empty directory")
    if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != manifest.get("sha256") or len(data) != manifest.get("bytes"):
        raise ValueError("backup checksum mismatch")
    if _SECRET.search(data.decode("utf-8")):
        raise ValueError("unsafe restore data")
    record = json.loads(data)
    if record.get("schema") not in (1, 2, 3, 4) or not re.fullmatch(r"[0-9a-f]{40}", record.get("source_commit", "")):
        raise ValueError("unsupported backup schema")
    if record["source_commit"] != manifest.get("source_commit") or len(record["jobs"]) != manifest.get("jobs") or len(record["steps"]) != manifest.get("steps"):
        raise ValueError("backup manifest mismatch")
    if record["schema"] >= 2 and len(record["evidence"]) != manifest.get("evidence"):
        raise ValueError("backup evidence manifest mismatch")
    if record["schema"] >= 3:
        for key in ("batches", "cases", "reviews", "findings", "finding_packets"):
            if len(record[key]) != manifest.get(key):
                raise ValueError("backup Phase 5 manifest mismatch")
    if record["schema"] >= 4 and len(record["feedback"]) != manifest.get("feedback"):
        raise ValueError("backup Phase 6 feedback manifest mismatch")
    store = Store(root)
    try:
        with store.transaction() as db:
            for j in record["jobs"]:
                db.execute("INSERT INTO ai_ops_jobs(id,status,priority,created,updated) VALUES(?,?,?,?,?)",
                           (_id(j["id"]), j["status"], j["priority"], j["created"], j["updated"]))
            for s in record["steps"]:
                db.execute("""INSERT INTO ai_ops_steps(job_id,step_id,status,input_hash,output_hash,output_json,attempts)
                    VALUES(?,?,?,?,?,?,?)""", (_id(s["job_id"]), _id(s["step_id"]), s["status"], s["input_hash"], s["output_hash"], None, s["attempts"]))
            for c in record["checkpoints"]:
                db.execute("INSERT INTO ai_ops_scheduler(name,checkpoint,updated) VALUES(?,?,0)", (_id(c["name"]), _id(c["checkpoint"])))
            for e in record.get("evidence", []):
                packet = _safe(e["packet"])
                content = json.dumps(packet, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(content.encode()).hexdigest()
                if digest != e["packet_id"]:
                    raise ValueError("restored evidence checksum mismatch")
                db.execute("""INSERT INTO ai_ops_evidence
                    (packet_id,window_start,case_ref,kind,packet_json,content_hash,created)
                    VALUES(?,?,?,?,?,?,0)""", (digest, e["window_start"], _id(e["case_ref"]), _id(e["kind"]), content, digest))
            if record["schema"] >= 3:
                for b in record["batches"]:
                    db.execute("""INSERT INTO ai_ops_batches(batch_id,role,packet_ids_json,status,created,updated)
                        VALUES(?,?,?,?,?,?)""", (_id(b["batch_id"]), _id(b["role"]), json.dumps([_id(v) for v in b["packet_ids"]], separators=(",", ":")),
                        _id(b["status"]), b["created"], b["updated"]))
                # Findings before cases preserves previous-finding links and case references.
                for f in record["findings"]:
                    compact_resolution = f.get("resolution")
                    resolution_json = json.dumps(_resolution_compact(json.dumps(compact_resolution, sort_keys=True, separators=(",", ":"))), sort_keys=True, separators=(",", ":")) if compact_resolution else None
                    db.execute("""INSERT INTO ai_ops_findings(finding_id,signature,occurrence,status,classification,severity,subsystem,case_ref,
                        previous_finding_id,fix_commit,fix_version,resolution_json,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (_id(f["finding_id"]), f["signature"], f["occurrence"], _id(f["status"]), _id(f["classification"]), _id(f["severity"]),
                         _id(f["subsystem"]), _id(f["case_ref"]), _id(f["previous_finding_id"]) if f.get("previous_finding_id") else None,
                         f.get("fix_commit"), f.get("fix_version"), resolution_json, f["created"], f["updated"]))
                for c in record["cases"]:
                    db.execute("""INSERT INTO ai_ops_cases(packet_id,state,pending_role,classification,severity,validation_status,finding_id,batch_id,
                        retry_count,last_error,created,updated,escalation_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (_id(c["packet_id"]), _id(c["state"]), _id(c["pending_role"]) if c.get("pending_role") else "",
                         _id(c["classification"]) if c.get("classification") else None, _id(c["severity"]) if c.get("severity") else None,
                         _id(c["validation_status"]) if c.get("validation_status") else None, _id(c["finding_id"]) if c.get("finding_id") else None,
                         _id(c["batch_id"]) if c.get("batch_id") else None, c["retry_count"], _id(c["last_error"]) if c.get("last_error") else None,
                         c["created"], c["updated"], _id(c.get("escalation_reason", "")) if c.get("escalation_reason") else ""))
                for r in record["reviews"]:
                    compact = _review_compact(json.dumps(r["result"], sort_keys=True, separators=(",", ":")))
                    db.execute("""INSERT INTO ai_ops_reviews(review_id,packet_id,role,provider,model,key_slot,classification,severity,
                        validation_status,result_json,agreement,disposition,created,independence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (_id(r["review_id"]), _id(r["packet_id"]), _id(r["role"]), _id(r["provider"]), _id(r["model"]), _id(r["key_slot"]),
                         _id(r["classification"]), _id(r["severity"]), _id(r["validation_status"]),
                         json.dumps(compact, sort_keys=True, separators=(",", ":")), _id(r["agreement"]) if r.get("agreement") else None,
                         _id(r["disposition"]) if r.get("disposition") else None, r["created"],
                         _id(r.get("independence", "NOT_APPLICABLE"))))
                for fp in record["finding_packets"]:
                    db.execute("INSERT INTO ai_ops_finding_packets(finding_id,packet_id,created) VALUES(?,?,?)",
                               (_id(fp["finding_id"]), _id(fp["packet_id"]), fp["created"]))
                for fb in record.get("feedback", []):
                    db.execute("INSERT INTO ai_ops_feedback(finding_id,verdict,updated) VALUES(?,?,?)",
                               (_id(fb["finding_id"]), _id(fb["verdict"]), fb["updated"]))
        if store.health()["integrity"] != "ok":
            raise RuntimeError("restored database failed integrity check")
        return store
    except BaseException:
        store.close()
        raise
