"""Grounded, free-only Supervisor for the Private AI Operations control plane.

The Supervisor reads durable AI Ops state and may explain/coordinate it. It has no
flight-decision authority and no production-mutation tools. Conversation continuity
is persisted on the dedicated AI Ops volume without retaining hidden reasoning.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .provider_adapters import ProviderFailure, Slot

SUPERVISOR_SCHEMA_VERSION = 1
MAX_MESSAGE_BYTES = 8192
MAX_TOOL_RESULT_BYTES = 16384
MAX_HISTORY = 24
MAX_TOOL_ROUNDS = 2
CLOUDFLARE_DAILY_NEURON_BUDGET = 8500

# Current Workers-Free allow-list. Rates are neurons per one million tokens and
# are used only for a conservative local free-allocation guard.
CLOUDFLARE_FREE_MODELS: dict[str, tuple[int, int]] = {
    "@cf/zai-org/glm-4.7-flash": (5500, 36400),
    "@cf/google/gemma-4-26b-a4b-it": (9091, 27273),
    "@cf/nvidia/nemotron-3-120b-a12b": (45455, 136364),
}
FALLBACK_FREE_MODELS = {
    "groq": "openai/gpt-oss-120b",
    "gemini": "gemini-3.8-flash",
    "openrouter": "openrouter/free",
}
_SECRET = re.compile(
    r"(?i)(authorization\s*:|bearer\s+[A-Za-z0-9._-]{8,}|mongodb(?:\+srv)?://|"
    r"gh[pousr]_[A-Za-z0-9]{10,}|sk-[A-Za-z0-9]{8,}|api[_-]?key\s*[:=]|"
    r"password\s*[:=]|refresh[_-]?token\s*[:=]|secret\s*[:=])"
)
_RECORD_ID = re.compile(r"\b(?:task|audit|p5|op|finding|case):[A-Za-z0-9:_-]{1,120}\b")
_SAFE_ID = re.compile(r"^[A-Za-z0-9:._/-]{1,160}$")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _redact(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return "[bounded]"
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in (
                "token", "secret", "password", "authorization", "api_key", "apikey",
                "mongo_uri", "credential", "latitude", "longitude", "observer_lat",
                "observer_lon", "receiver_lat", "receiver_lon",
            )):
                out[key] = "[redacted]"
            else:
                out[key] = _redact(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        if _SECRET.search(value):
            return "[redacted]"
        return value if len(value) <= 4096 else value[:4096] + "…[truncated]"
    return value


def _bounded_text(value: str, *, limit: int = MAX_MESSAGE_BYTES) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("non-empty message required")
    raw = value.strip()
    if len(raw.encode()) > limit:
        raise ValueError("message exceeds bounded Supervisor limit")
    return raw


def _utc_day(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m-%d")


def cloudflare_slot_label(slot: Slot) -> str:
    if slot.provider != "cloudflare":
        return slot.name
    return "Slot 2" if slot.name.endswith("_2") else "Slot 1"


class SupervisorStore:
    """Small auxiliary SQLite store on the same dedicated persistent volume."""

    def __init__(self, data_dir: str | Path):
        root = Path(data_dir).resolve()
        if not root.is_dir():
            raise FileNotFoundError("dedicated persistent data directory required")
        self.path = root / "supervisor.sqlite"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS supervisor_meta(
                  key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_conversations(
                  conversation_id TEXT PRIMARY KEY,
                  provider TEXT, model TEXT, slot TEXT,
                  status TEXT NOT NULL DEFAULT 'ACTIVE', topic TEXT NOT NULL DEFAULT '',
                  continuity_json TEXT NOT NULL DEFAULT '{}', backend_version TEXT NOT NULL DEFAULT '',
                  created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_messages(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT NOT NULL REFERENCES supervisor_conversations(conversation_id) ON DELETE CASCADE,
                  role TEXT NOT NULL CHECK(role IN ('user','assistant')),
                  content TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS supervisor_messages_conversation
                  ON supervisor_messages(conversation_id,id);
                CREATE TABLE IF NOT EXISTS supervisor_tool_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT NOT NULL REFERENCES supervisor_conversations(conversation_id) ON DELETE CASCADE,
                  tool TEXT NOT NULL, arguments_json TEXT NOT NULL, result_json TEXT NOT NULL,
                  result_hash TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS supervisor_tool_conversation
                  ON supervisor_tool_events(conversation_id,id);
                CREATE TABLE IF NOT EXISTS supervisor_switches(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT NOT NULL REFERENCES supervisor_conversations(conversation_id) ON DELETE CASCADE,
                  from_provider TEXT, from_model TEXT, from_slot TEXT,
                  to_provider TEXT, to_model TEXT, to_slot TEXT,
                  reason TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_usage(
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  conversation_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL, slot TEXT NOT NULL,
                  utc_day TEXT NOT NULL, input_tokens INTEGER NOT NULL DEFAULT 0,
                  output_tokens INTEGER NOT NULL DEFAULT 0, estimated_neurons INTEGER NOT NULL DEFAULT 0,
                  success INTEGER NOT NULL, failure_kind TEXT, latency_ms INTEGER NOT NULL,
                  created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS supervisor_usage_day
                  ON supervisor_usage(provider,slot,utc_day);
                CREATE TABLE IF NOT EXISTS supervisor_benchmarks(
                  run_id TEXT NOT NULL, model TEXT NOT NULL, score_json TEXT NOT NULL,
                  latency_ms INTEGER NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
                  created REAL NOT NULL, PRIMARY KEY(run_id,model)
                );
            """)
            db.execute(
                "INSERT OR REPLACE INTO supervisor_meta(key,value) VALUES('schema_version',?)",
                (str(SUPERVISOR_SCHEMA_VERSION),),
            )

    def create_conversation(self, conversation_id: str | None = None) -> str:
        cid = conversation_id or ("sup-" + secrets.token_urlsafe(12))
        if not _SAFE_ID.fullmatch(cid):
            raise ValueError("invalid conversation id")
        now = time.time()
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO supervisor_conversations(conversation_id,created,updated) VALUES(?,?,?)",
                (cid, now, now),
            )
        return cid

    def conversation(self, conversation_id: str) -> dict[str, object] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM supervisor_conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        try:
            item["continuity"] = _redact(json.loads(str(item.pop("continuity_json"))))
        except (ValueError, TypeError):
            item["continuity"] = {}
        return item

    def append_message(self, conversation_id: str, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError("invalid retained role")
        text = _bounded_text(content)
        if _SECRET.search(text):
            raise ValueError("secret-like content cannot be retained in Supervisor chat")
        now = time.time()
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM supervisor_conversations WHERE conversation_id=?", (conversation_id,)).fetchone():
                raise KeyError("unknown conversation")
            db.execute(
                "INSERT INTO supervisor_messages(conversation_id,role,content,created) VALUES(?,?,?,?)",
                (conversation_id, role, text, now),
            )
            db.execute("UPDATE supervisor_conversations SET updated=? WHERE conversation_id=?", (now, conversation_id))
            db.execute("""DELETE FROM supervisor_messages WHERE conversation_id=? AND id NOT IN (
                SELECT id FROM supervisor_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 100)""",
                (conversation_id, conversation_id),
            )

    def messages(self, conversation_id: str, *, limit: int = MAX_HISTORY) -> list[dict[str, object]]:
        limit = max(1, min(limit, 100))
        with self._connect() as db:
            rows = db.execute(
                "SELECT role,content,created FROM supervisor_messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?", (conversation_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def set_affinity(self, conversation_id: str, provider: str, model: str, slot: str, *, reason: str) -> None:
        now = time.time()
        with self._connect() as db:
            old = db.execute(
                "SELECT provider,model,slot FROM supervisor_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            if not old:
                raise KeyError("unknown conversation")
            changed = (old["provider"], old["model"], old["slot"]) != (provider, model, slot)
            if changed and old["provider"]:
                db.execute("""INSERT INTO supervisor_switches(
                    conversation_id,from_provider,from_model,from_slot,to_provider,to_model,to_slot,reason,created)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (conversation_id, old["provider"], old["model"], old["slot"], provider, model, slot, reason[:80], now),
                )
            db.execute(
                "UPDATE supervisor_conversations SET provider=?,model=?,slot=?,status='ACTIVE',updated=? WHERE conversation_id=?",
                (provider, model, slot, now, conversation_id),
            )

    def set_degraded(self, conversation_id: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE supervisor_conversations SET status='DEGRADED',updated=? WHERE conversation_id=?",
                (time.time(), conversation_id),
            )

    def update_continuity(self, conversation_id: str, value: Mapping[str, object], backend_version: str) -> None:
        sanitized = _redact(dict(value))
        encoded = _json(sanitized)
        if len(encoded.encode()) > 32768:
            raise ValueError("continuity packet too large")
        with self._connect() as db:
            db.execute(
                "UPDATE supervisor_conversations SET continuity_json=?,backend_version=?,updated=? WHERE conversation_id=?",
                (encoded, backend_version[:128], time.time(), conversation_id),
            )

    def record_tool(self, conversation_id: str, tool: str, arguments: Mapping[str, object], result: object) -> None:
        args = _json(_redact(dict(arguments)))
        result_json = _json(_redact(result))
        if len(args.encode()) > 4096 or len(result_json.encode()) > MAX_TOOL_RESULT_BYTES:
            raise ValueError("bounded Supervisor tool artifact required")
        digest = hashlib.sha256(result_json.encode()).hexdigest()
        with self._connect() as db:
            db.execute(
                "INSERT INTO supervisor_tool_events(conversation_id,tool,arguments_json,result_json,result_hash,created) VALUES(?,?,?,?,?,?)",
                (conversation_id, tool[:80], args, result_json, digest, time.time()),
            )
            db.execute("""DELETE FROM supervisor_tool_events WHERE conversation_id=? AND id NOT IN (
                SELECT id FROM supervisor_tool_events WHERE conversation_id=? ORDER BY id DESC LIMIT 100)""",
                (conversation_id, conversation_id),
            )

    def switch_history(self, conversation_id: str) -> list[dict[str, object]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT from_provider,from_model,from_slot,to_provider,to_model,to_slot,reason,created "
                "FROM supervisor_switches WHERE conversation_id=? ORDER BY id", (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_today(self, provider: str, slot: str, *, now: float | None = None) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT coalesce(sum(estimated_neurons),0) FROM supervisor_usage "
                "WHERE provider=? AND slot=? AND utc_day=? AND success=1",
                (provider, slot, _utc_day(now)),
            ).fetchone()
        return int(row[0])

    def record_usage(self, conversation_id: str, provider: str, model: str, slot: str,
                     input_tokens: int, output_tokens: int, estimated_neurons: int,
                     *, success: bool, latency_ms: int, failure_kind: str | None = None) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO supervisor_usage(
                conversation_id,provider,model,slot,utc_day,input_tokens,output_tokens,
                estimated_neurons,success,failure_kind,latency_ms,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (conversation_id, provider, model, slot, _utc_day(), max(0, input_tokens),
                 max(0, output_tokens), max(0, estimated_neurons), 1 if success else 0,
                 failure_kind, max(0, latency_ms), time.time()),
            )
            db.execute("DELETE FROM supervisor_usage WHERE created<?", (time.time() - 90 * 86400,))


class SupervisorBackend:
    """Structured read-only tools over the canonical AI Ops SQLite state."""

    TOOL_NAMES = (
        "get_current_tasks", "get_task", "get_recent_findings", "get_finding",
        "get_agent_activity", "get_provider_health", "get_task_timeline",
        "get_test_results", "get_replay_results", "get_tool_operations",
        "get_ai_reviews", "get_deployments", "get_git_changes", "get_backup_status",
    )

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).resolve()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=2000")
        return db

    @staticmethod
    def _rows(db: sqlite3.Connection, sql: str, params: Sequence[object] = ()) -> list[dict[str, object]]:
        return [_redact(dict(row)) for row in db.execute(sql, tuple(params)).fetchall()]

    def get_current_tasks(self, limit: int = 20) -> dict[str, object]:
        limit = max(1, min(int(limit), 50))
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT id,status,priority,created,updated,lease_owner,lease_until FROM ai_ops_jobs "
                "WHERE status IN ('PENDING','RETRY','RUNNING') ORDER BY priority DESC,created LIMIT ?", (limit,))
        return {"available": True, "tasks": rows}

    def get_task(self, task_id: str) -> dict[str, object]:
        if not _SAFE_ID.fullmatch(task_id):
            return {"available": False, "reason": "invalid_task_id"}
        with self._connect() as db:
            row = db.execute("SELECT * FROM ai_ops_jobs WHERE id=?", (task_id,)).fetchone()
            if not row:
                return {"available": False, "reason": "task_not_found", "task_id": task_id}
            steps = self._rows(db,
                "SELECT step_id,status,input_hash,output_hash,attempts,lease_owner,lease_until FROM ai_ops_steps WHERE job_id=? ORDER BY rowid",
                (task_id,))
        return {"available": True, "task": _redact(dict(row)), "steps": steps}

    def get_recent_findings(self, limit: int = 20) -> dict[str, object]:
        limit = max(1, min(int(limit), 50))
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT finding_id,severity,status,classification,subsystem,case_ref,fix_commit,fix_version,created,updated "
                "FROM ai_ops_findings ORDER BY updated DESC LIMIT ?", (limit,))
        return {"available": True, "findings": rows}

    def get_finding(self, finding_id: str) -> dict[str, object]:
        if not _SAFE_ID.fullmatch(finding_id):
            return {"available": False, "reason": "invalid_finding_id"}
        with self._connect() as db:
            row = db.execute("SELECT * FROM ai_ops_findings WHERE finding_id=?", (finding_id,)).fetchone()
            if not row:
                return {"available": False, "reason": "finding_not_found", "finding_id": finding_id}
            reviews = self._rows(db,
                "SELECT r.review_id,r.packet_id,r.role,r.provider,r.model,r.key_slot,r.classification,r.severity,"
                "r.validation_status,r.agreement,r.disposition,r.independence,r.created FROM ai_ops_reviews r "
                "WHERE r.packet_id IN (SELECT packet_id FROM ai_ops_finding_packets WHERE finding_id=?) ORDER BY r.created",
                (finding_id,))
            tools = self._rows(db,
                "SELECT operation_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
                "FROM ai_ops_tool_operations WHERE finding_id=? ORDER BY created", (finding_id,))
        return {"available": True, "finding": _redact(dict(row)), "reviews": reviews, "tool_operations": tools}

    def get_agent_activity(self, limit: int = 20) -> dict[str, object]:
        limit = max(1, min(int(limit), 50))
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT id,status,lease_owner,lease_until,created,updated FROM ai_ops_jobs "
                "WHERE status='RUNNING' ORDER BY updated DESC LIMIT ?", (limit,))
        return {"available": True, "agents": rows}

    def get_provider_health(self) -> dict[str, object]:
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT slot,provider,attempts,successes,failures,input_tokens,output_tokens,consecutive_failures,"
                "cooldown_until,last_status,updated FROM ai_ops_provider_health ORDER BY provider,slot")
        return {"available": True, "providers": rows}

    def get_task_timeline(self, task_id: str) -> dict[str, object]:
        base = self.get_task(task_id)
        if not base.get("available"):
            return base
        with self._connect() as db:
            attempts = self._rows(db,
                "SELECT id,step_id,worker,started,ended,result FROM ai_ops_attempts WHERE job_id=? ORDER BY id", (task_id,))
            tools = self._rows(db,
                "SELECT operation_id,tool,status,source_commit,started,finished,result_class,output_hash,artifact_hash,created "
                "FROM ai_ops_tool_operations WHERE job_id=? ORDER BY created", (task_id,))
        return {"available": True, "task_id": task_id, "steps": base["steps"], "attempts": attempts, "tool_operations": tools}

    def _tool_results(self, pattern: str, task_id: str | None = None) -> dict[str, object]:
        with self._connect() as db:
            if task_id:
                rows = self._rows(db,
                    "SELECT operation_id,job_id,finding_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
                    "FROM ai_ops_tool_operations WHERE job_id=? AND lower(tool) LIKE ? ORDER BY created DESC LIMIT 50",
                    (task_id, pattern))
            else:
                rows = self._rows(db,
                    "SELECT operation_id,job_id,finding_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
                    "FROM ai_ops_tool_operations WHERE lower(tool) LIKE ? ORDER BY created DESC LIMIT 50", (pattern,))
        return {"available": True, "results": rows}

    def get_test_results(self, task_id: str | None = None) -> dict[str, object]:
        return self._tool_results("%test%", task_id)

    def get_replay_results(self, task_id: str | None = None) -> dict[str, object]:
        return self._tool_results("%replay%", task_id)

    def get_tool_operations(self, task_id: str | None = None, limit: int = 30) -> dict[str, object]:
        limit = max(1, min(int(limit), 50))
        with self._connect() as db:
            if task_id:
                rows = self._rows(db,
                    "SELECT operation_id,job_id,finding_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
                    "FROM ai_ops_tool_operations WHERE job_id=? ORDER BY created DESC LIMIT ?", (task_id, limit))
            else:
                rows = self._rows(db,
                    "SELECT operation_id,job_id,finding_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
                    "FROM ai_ops_tool_operations ORDER BY created DESC LIMIT ?", (limit,))
        return {"available": True, "operations": rows}

    def get_ai_reviews(self, finding_id: str | None = None, limit: int = 30) -> dict[str, object]:
        limit = max(1, min(int(limit), 50))
        with self._connect() as db:
            if finding_id:
                rows = self._rows(db,
                    "SELECT r.review_id,r.packet_id,r.role,r.provider,r.model,r.key_slot,r.classification,r.severity,"
                    "r.validation_status,r.agreement,r.disposition,r.independence,r.created FROM ai_ops_reviews r "
                    "WHERE r.packet_id IN (SELECT packet_id FROM ai_ops_finding_packets WHERE finding_id=?) "
                    "ORDER BY r.created DESC LIMIT ?", (finding_id, limit))
            else:
                rows = self._rows(db,
                    "SELECT review_id,packet_id,role,provider,model,key_slot,classification,severity,validation_status,"
                    "agreement,disposition,independence,created FROM ai_ops_reviews ORDER BY created DESC LIMIT ?", (limit,))
        return {"available": True, "reviews": rows}

    def get_deployments(self) -> dict[str, object]:
        return {"available": False, "reason": "deployment_records_not_persisted_in_ai_ops_backend"}

    def get_git_changes(self) -> dict[str, object]:
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT operation_id,job_id,tool,source_commit,status,artifact_hash,created FROM ai_ops_tool_operations "
                "WHERE tool IN ('candidate_patch','candidate_regression_test','source_read','source_search') "
                "ORDER BY created DESC LIMIT 30")
        return {"available": bool(rows), "changes": rows, "reason": None if rows else "no_persisted_git_artifacts"}

    def get_backup_status(self) -> dict[str, object]:
        with self._connect() as db:
            rows = self._rows(db,
                "SELECT destination,revision,synced_revision,last_attempt,last_verified,last_hash,next_attempt,retry_count "
                "FROM ai_ops_dr_sync ORDER BY destination")
        return {"available": True, "destinations": rows}

    def call(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        if name not in self.TOOL_NAMES:
            return {"available": False, "reason": "unknown_tool", "tool": name}
        args = dict(arguments or {})
        try:
            if name == "get_current_tasks": return self.get_current_tasks(int(args.get("limit", 20)))
            if name == "get_task": return self.get_task(str(args.get("task_id", "")))
            if name == "get_recent_findings": return self.get_recent_findings(int(args.get("limit", 20)))
            if name == "get_finding": return self.get_finding(str(args.get("finding_id", "")))
            if name == "get_agent_activity": return self.get_agent_activity(int(args.get("limit", 20)))
            if name == "get_provider_health": return self.get_provider_health()
            if name == "get_task_timeline": return self.get_task_timeline(str(args.get("task_id", "")))
            if name == "get_test_results": return self.get_test_results(str(args["task_id"]) if args.get("task_id") else None)
            if name == "get_replay_results": return self.get_replay_results(str(args["task_id"]) if args.get("task_id") else None)
            if name == "get_tool_operations": return self.get_tool_operations(str(args["task_id"]) if args.get("task_id") else None, int(args.get("limit", 30)))
            if name == "get_ai_reviews": return self.get_ai_reviews(str(args["finding_id"]) if args.get("finding_id") else None, int(args.get("limit", 30)))
            if name == "get_deployments": return self.get_deployments()
            if name == "get_git_changes": return self.get_git_changes()
            if name == "get_backup_status": return self.get_backup_status()
        except (ValueError, TypeError, sqlite3.Error):
            return {"available": False, "reason": "backend_tool_error", "tool": name}
        return {"available": False, "reason": "backend_tool_error", "tool": name}

    def tool_definitions(self) -> list[dict[str, object]]:
        definitions = []
        for name in self.TOOL_NAMES:
            properties: dict[str, object] = {}
            if name in ("get_task", "get_task_timeline"):
                properties["task_id"] = {"type": "string"}
            elif name == "get_finding":
                properties["finding_id"] = {"type": "string"}
            elif name in ("get_test_results", "get_replay_results", "get_tool_operations"):
                properties["task_id"] = {"type": "string"}
            elif name == "get_ai_reviews":
                properties["finding_id"] = {"type": "string"}
            if name in ("get_current_tasks", "get_recent_findings", "get_agent_activity", "get_tool_operations", "get_ai_reviews"):
                properties["limit"] = {"type": "integer", "minimum": 1, "maximum": 50}
            definitions.append({"type": "function", "function": {
                "name": name,
                "description": "Read current durable Plane Alerts AI Ops state. Never mutates production.",
                "parameters": {"type": "object", "properties": properties, "additionalProperties": False},
            }})
        return definitions

    def snapshot(self, user_text: str) -> dict[str, object]:
        snapshot: dict[str, object] = {
            "current_tasks": self.get_current_tasks(20),
            "recent_findings": self.get_recent_findings(20),
            "agent_activity": self.get_agent_activity(20),
            "provider_health": self.get_provider_health(),
            "recent_tests": self.get_test_results(),
            "recent_replays": self.get_replay_results(),
            "backup_status": self.get_backup_status(),
        }
        for record_id in sorted(set(_RECORD_ID.findall(user_text)))[:8]:
            if record_id.startswith(("task:", "audit:", "p5:")):
                snapshot.setdefault("referenced_tasks", {})[record_id] = self.get_task(record_id)
            elif record_id.startswith("finding:"):
                snapshot.setdefault("referenced_findings", {})[record_id] = self.get_finding(record_id)
        return _redact(snapshot)


@dataclass(frozen=True)
class ChatResult:
    content: str | None
    tool_calls: tuple[dict[str, object], ...]
    input_tokens: int
    output_tokens: int


class SupervisorTransport:
    """Bounded chat transport. Raw provider error bodies are never returned or persisted."""

    def chat(self, slot: Slot, model: str, messages: Sequence[Mapping[str, object]], *,
             tools: Sequence[Mapping[str, object]] = (), affinity: str = "") -> ChatResult:
        try:
            import httpx
        except ImportError as exc:
            raise ProviderFailure("network") from exc
        headers = {"Content-Type": "application/json"}
        payload: dict[str, object]
        if slot.provider == "cloudflare":
            if model not in CLOUDFLARE_FREE_MODELS or not re.fullmatch(r"[0-9a-fA-F]{32}", slot.account_id):
                raise ValueError("unapproved Cloudflare Supervisor route")
            url = f"https://api.cloudflare.com/client/v4/accounts/{slot.account_id}/ai/v1/chat/completions"
            headers["Authorization"] = "Bearer " + slot.credential
            if affinity:
                headers["x-session-affinity"] = affinity[:128]
            payload = {"model": model, "messages": list(messages), "max_tokens": 700,
                       "temperature": 0, "options": {"rejectIfBusy": True}}
            if tools:
                payload["tools"] = list(tools)
                payload["tool_choice"] = "auto"
        elif slot.provider in ("groq", "mistral", "openrouter"):
            if slot.provider == "openrouter" and not (model.endswith(":free") or model == "openrouter/free"):
                raise ValueError("paid OpenRouter route refused")
            url = {"groq": "https://api.groq.com/openai/v1/chat/completions",
                   "mistral": "https://api.mistral.ai/v1/chat/completions",
                   "openrouter": "https://openrouter.ai/api/v1/chat/completions"}[slot.provider]
            headers["Authorization"] = "Bearer " + slot.credential
            payload = {"model": model, "messages": list(messages), "max_tokens": 700, "temperature": 0}
            if tools:
                payload["tools"] = list(tools)
                payload["tool_choice"] = "auto"
        elif slot.provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent"
            headers["x-goog-api-key"] = slot.credential
            text = "\n".join(f"{m.get('role','user').upper()}: {m.get('content','')}" for m in messages if isinstance(m.get("content"), str))
            payload = {"contents": [{"parts": [{"text": text}]}],
                       "generationConfig": {"maxOutputTokens": 700, "temperature": 0}}
        else:
            raise ValueError("unsupported Supervisor provider")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(12.0), follow_redirects=False) as client:
                response = client.post(url, headers=headers, content=_json(payload).encode())
                status = response.status_code
                raw = response.content[:131073]
        except httpx.TimeoutException:
            raise ProviderFailure("timeout") from None
        except httpx.HTTPError:
            raise ProviderFailure("network") from None
        if status != 200:
            kind = "quota" if status == 429 else "auth" if status in (401, 403) else "server" if status >= 500 else "request"
            raise ProviderFailure(kind, status=status, retry_after=60 if status == 429 else 0,
                                  provider_wide=status == 429)
        if len(raw) > 131072:
            raise ProviderFailure("malformed")
        try:
            envelope = json.loads(raw)
            if slot.provider == "gemini":
                content = envelope["candidates"][0]["content"]["parts"][0]["text"]
                usage = envelope.get("usageMetadata", {})
                incoming = int(usage.get("promptTokenCount", 0) or 0)
                outgoing = int(usage.get("candidatesTokenCount", 0) or 0)
                calls: tuple[dict[str, object], ...] = ()
            else:
                message = envelope["choices"][0]["message"]
                content = message.get("content")
                raw_calls = message.get("tool_calls") or []
                calls = tuple(dict(call) for call in raw_calls[:8] if isinstance(call, dict))
                usage = envelope.get("usage", {})
                incoming = int(usage.get("prompt_tokens", 0) or 0)
                outgoing = int(usage.get("completion_tokens", 0) or 0)
            if content is not None and (not isinstance(content, str) or len(content.encode()) > MAX_MESSAGE_BYTES):
                raise ValueError("invalid content")
            if incoming < 0 or outgoing < 0 or incoming > 1_000_000 or outgoing > 100_000:
                raise ValueError("invalid usage")
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            raise ProviderFailure("malformed") from None
        _ = int((time.monotonic() - started) * 1000)
        return ChatResult(content, calls, incoming, outgoing)


class SupervisorEngine:
    def __init__(self, backend: SupervisorBackend, state: SupervisorStore, slots: Iterable[Slot], *,
                 cloudflare_model: str, transport: SupervisorTransport | None = None,
                 fallback_models: Mapping[str, str] | None = None):
        if cloudflare_model not in CLOUDFLARE_FREE_MODELS:
            raise ValueError("Supervisor Cloudflare model is not approved Workers-Free")
        self.backend = backend
        self.state = state
        self.slots = tuple(slots)
        self.cloudflare_model = cloudflare_model
        self.transport = transport or SupervisorTransport()
        self.fallback_models = dict(FALLBACK_FREE_MODELS if fallback_models is None else fallback_models)
        if self.fallback_models.get("openrouter") and not (
            self.fallback_models["openrouter"].endswith(":free") or self.fallback_models["openrouter"] == "openrouter/free"
        ):
            raise ValueError("paid OpenRouter fallback refused")

    def new_conversation(self) -> str:
        return self.state.create_conversation()

    def _candidate_routes(self, conversation: Mapping[str, object], *, explicit_switch: bool) -> list[tuple[Slot, str]]:
        routes: list[tuple[Slot, str]] = []
        current = (conversation.get("provider"), conversation.get("model"), conversation.get("slot"))
        if current[0] and not explicit_switch:
            for slot in self.slots:
                if slot.provider == current[0] and slot.name == current[2]:
                    routes.append((slot, str(current[1])))
                    break
        cloudflare = [s for s in self.slots if s.provider == "cloudflare"]
        cloudflare.sort(key=lambda s: 1 if s.name.endswith("_2") else 0)
        for slot in cloudflare:
            candidate = (slot, self.cloudflare_model)
            if candidate not in routes:
                routes.append(candidate)
        for provider in ("groq", "gemini", "openrouter"):
            model = self.fallback_models.get(provider)
            if not model:
                continue
            for slot in self.slots:
                if slot.provider == provider:
                    candidate = (slot, model)
                    if candidate not in routes:
                        routes.append(candidate)
                    break
        return routes

    @staticmethod
    def _estimated_neurons(model: str, input_tokens: int, output_tokens: int) -> int:
        rates = CLOUDFLARE_FREE_MODELS.get(model)
        if not rates:
            return 0
        return int(math.ceil((input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000))

    def _cloudflare_budget_allows(self, slot: Slot, model: str, snapshot: object, history: Sequence[Mapping[str, object]]) -> bool:
        if slot.provider != "cloudflare":
            return True
        rough_input = max(1, (len(_json(snapshot)) + len(_json(list(history)))) // 3)
        reserve = self._estimated_neurons(model, rough_input, 700)
        used = self.state.usage_today("cloudflare", cloudflare_slot_label(slot))
        return used + reserve <= CLOUDFLARE_DAILY_NEURON_BUDGET

    @staticmethod
    def _system_prompt(snapshot: Mapping[str, object]) -> str:
        return (
            "You are the private Plane Alerts Operations Supervisor. Answer only from the BACKEND_SNAPSHOT and read tools. "
            "If evidence is missing, explicitly say it is unavailable; never invent tasks, findings, replays, tests, deployments, or provider state. "
            "Never reveal credentials or private coordinates. Never provide hidden chain-of-thought; give concise decision/rationale summaries only. "
            "You have no authority to alter aircraft trajectory, CPA, ETA/TTC, pass/no-pass, alert qualification/cancellation, route guards, or alert timing. "
            "You also have no merge/deploy/secret/config mutation authority. Read tools are safe and current.\n"
            "BACKEND_SNAPSHOT=" + _json(snapshot)
        )

    @staticmethod
    def _known_ids(value: object) -> set[str]:
        result: set[str] = set()
        def walk(item: object) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, str) and (key.endswith("id") or key.endswith("_id")) and _SAFE_ID.fullmatch(child):
                        result.add(child)
                    walk(child)
            elif isinstance(item, list):
                for child in item: walk(child)
        walk(value)
        return result

    def _validated_answer(self, content: str | None, evidence: object) -> str:
        if not content:
            return "The Supervisor provider returned no answer. Current backend state remains available in the GUI."
        text = content.strip()
        if len(text.encode()) > MAX_MESSAGE_BYTES:
            text = text.encode()[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore")
        if _SECRET.search(text):
            return "The provider response was withheld because it contained secret-like material. No credential was exposed."
        known = self._known_ids(evidence)
        unknown = sorted({record for record in _RECORD_ID.findall(text) if record not in known})
        if unknown:
            return "I do not have durable backend evidence for the referenced record(s): " + ", ".join(unknown[:5]) + "."
        return text

    def chat(self, conversation_id: str, user_text: str) -> dict[str, object]:
        text = _bounded_text(user_text)
        if _SECRET.search(text):
            return {"status": "REJECTED", "answer": "This message appears to contain a credential or secret. It was not sent to any AI provider."}
        conversation = self.state.conversation(conversation_id)
        if conversation is None:
            raise KeyError("unknown conversation")
        explicit_switch = any(phrase in text.lower() for phrase in ("switch provider", "ask another provider", "another ai"))
        snapshot = self.backend.snapshot(text)
        self.state.append_message(conversation_id, "user", text)
        history = self.state.messages(conversation_id, limit=MAX_HISTORY)
        base_messages: list[dict[str, object]] = [{"role": "system", "content": self._system_prompt(snapshot)}]
        base_messages.extend({"role": str(item["role"]), "content": str(item["content"])} for item in history)
        last_failure = "unavailable"
        previous_route = (conversation.get("provider"), conversation.get("model"), conversation.get("slot"))

        for slot, model in self._candidate_routes(conversation, explicit_switch=explicit_switch):
            label = cloudflare_slot_label(slot)
            if not self._cloudflare_budget_allows(slot, model, snapshot, history):
                last_failure = "free_budget_guard"
                continue
            messages = list(base_messages)
            evidence: dict[str, object] = {"snapshot": snapshot, "tools": []}
            started = time.monotonic()
            try:
                response = self.transport.chat(slot, model, messages, tools=self.backend.tool_definitions(), affinity=conversation_id)
                total_in, total_out = response.input_tokens, response.output_tokens
                for _round in range(MAX_TOOL_ROUNDS):
                    if not response.tool_calls:
                        break
                    assistant_calls = []
                    for index, call in enumerate(response.tool_calls):
                        call_id = str(call.get("id") or f"call-{index}")[:128]
                        function = call.get("function") if isinstance(call.get("function"), dict) else {}
                        name = str(function.get("name", ""))
                        try:
                            arguments = json.loads(str(function.get("arguments", "{}")))
                        except json.JSONDecodeError:
                            arguments = {}
                        if not isinstance(arguments, dict):
                            arguments = {}
                        result = self.backend.call(name, arguments)
                        self.state.record_tool(conversation_id, name, arguments, result)
                        evidence["tools"].append({"tool": name, "arguments": _redact(arguments), "result": _redact(result)})
                        assistant_calls.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": _json(arguments)}})
                        messages.append({"role": "tool", "tool_call_id": call_id, "content": _json(result)})
                    messages.insert(len(messages) - len(assistant_calls), {"role": "assistant", "content": response.content, "tool_calls": assistant_calls})
                    response = self.transport.chat(slot, model, messages, tools=self.backend.tool_definitions(), affinity=conversation_id)
                    total_in += response.input_tokens
                    total_out += response.output_tokens
                elapsed = int((time.monotonic() - started) * 1000)
                neurons = self._estimated_neurons(model, total_in, total_out) if slot.provider == "cloudflare" else 0
                self.state.record_usage(conversation_id, slot.provider, model, label, total_in, total_out, neurons,
                                        success=True, latency_ms=elapsed)
                reason = "explicit_user_request" if explicit_switch else "provider_failover" if previous_route[0] else "initial_selection"
                self.state.set_affinity(conversation_id, slot.provider, model, slot.name, reason=reason)
                answer = self._validated_answer(response.content, evidence)
                self.state.append_message(conversation_id, "assistant", answer)
                continuity = {
                    "conversation_id": conversation_id,
                    "provider": slot.provider,
                    "model": model,
                    "current_topic": text[:240],
                    "referenced_task_ids": sorted(x for x in self._known_ids(evidence) if x.startswith(("task:", "audit:", "p5:")))[:20],
                    "referenced_finding_ids": sorted(x for x in self._known_ids(evidence) if x.startswith("finding:"))[:20],
                    "verified_facts": [event["tool"] for event in evidence["tools"][:20]],
                    "completed_commands": [],
                    "pending_action": None,
                    "backend_snapshot": hashlib.sha256(_json(snapshot).encode()).hexdigest(),
                }
                self.state.update_continuity(conversation_id, continuity, backend_version=str(snapshot.get("backup_status", {}).get("available", "unknown")))
                return {"status": "OK", "answer": answer, "provider": slot.provider, "model": model,
                        "slot": label, "switch_history": self.state.switch_history(conversation_id)}
            except ProviderFailure as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                last_failure = exc.kind
                self.state.record_usage(conversation_id, slot.provider, model, label, 0, 0, 0,
                                        success=False, latency_ms=elapsed, failure_kind=exc.kind)
                continue
        self.state.set_degraded(conversation_id)
        answer = "Supervisor AI is currently unavailable. The private Operations GUI and durable backend remain available; no model reasoning was performed."
        self.state.append_message(conversation_id, "assistant", answer)
        return {"status": "DEGRADED", "answer": answer, "failure": last_failure,
                "switch_history": self.state.switch_history(conversation_id)}
