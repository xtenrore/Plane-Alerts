"""Phase 2 sanitized portable checkpoint; independent of any remote transport.

The GitHub destination must be a dedicated private repository. This module
does not upload anything and deliberately refuses sensitive or unknown fields.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .store import Store

_SAFE_ID = re.compile(r"^[A-Za-z0-9:_./-]{1,128}$")
_SAFE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 _.-]{0,200}$")
_SECRET = re.compile(r"(?i)(authorization|bearer\s|mongodb(?:\+srv)?://|gh[pousr]_|sk-[a-z0-9]|api[_-]?key|token|password|secret|\b(?:lat|lon|latitude|longitude)\b)")


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
    if isinstance(value, int) and 0 <= value <= 10000:
        return value
    raise ValueError("unsupported or private export value")


def _id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value) or _SECRET.search(value):
        raise ValueError("unsafe checkpoint identifier")
    return value


def export(store: Store, *, source_commit: str) -> tuple[bytes, dict[str, object]]:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("exact source commit required")
    if store.health()["integrity"] != "ok":
        raise RuntimeError("unhealthy database")
    jobs = []
    for job in store.db.execute("SELECT id,status,priority,created,updated FROM ai_ops_jobs ORDER BY id"):
        # A backup must never restore an obsolete worker lease. Interrupted work
        # enters the normal retry queue on the clean host.
        status = "RETRY" if job[1] == "RUNNING" else job[1]
        jobs.append({"id": _id(job[0]), "status": status, "priority": job[2], "created": job[3], "updated": job[4]})
    steps = []
    for step in store.db.execute("SELECT job_id,step_id,status,input_hash,output_hash,attempts FROM ai_ops_steps ORDER BY job_id,step_id"):
        # Step output can contain conversations, locations, or credentials.
        # The commitment hash is enough to skip completed idempotent work.
        steps.append({"job_id": _id(step[0]), "step_id": _id(step[1]), "status": "RETRY" if step[2] == "RUNNING" else step[2],
                      "input_hash": step[3], "output_hash": step[4], "attempts": step[5]})
    checkpoints = [{"name": _id(row[0]), "checkpoint": _id(row[1])}
                   for row in store.db.execute("SELECT name,checkpoint FROM ai_ops_scheduler ORDER BY name")]
    record = {"schema": 1, "source_commit": source_commit, "jobs": jobs, "steps": steps, "checkpoints": checkpoints}
    data = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > 2_000_000 or _SECRET.search(data.decode()):
        raise ValueError("sanitized export failed privacy gate")
    manifest = {"schema": 1, "source_commit": source_commit, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "jobs": len(jobs), "steps": len(steps)}
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
    if record.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{40}", record.get("source_commit", "")):
        raise ValueError("unsupported backup schema")
    if record["source_commit"] != manifest.get("source_commit") or len(record["jobs"]) != manifest.get("jobs") or len(record["steps"]) != manifest.get("steps"):
        raise ValueError("backup manifest mismatch")
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
        if store.health()["integrity"] != "ok":
            raise RuntimeError("restored database failed integrity check")
        return store
    except BaseException:
        store.close()
        raise
