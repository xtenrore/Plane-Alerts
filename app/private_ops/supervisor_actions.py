"""Confirmation-gated safe actions for the owner-only Operations Supervisor.

The model may propose bounded analysis actions, but it cannot confirm its own
proposal. Confirmation is accepted only when the current authenticated owner
message contains the exact durable action id and explicit approval wording.
No merge, deployment, secret/config mutation, or flight-decision action exists.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping

from .phase5 import ACTIVE_FINDINGS, queue_review_batches
from .store import QueueFull, Store
from .supervisor import SupervisorBackend, SupervisorEngine

ACTIONS = frozenset({
    "start_investigation",
    "request_reviewer",
    "retry_failed_task",
    "pause_job",
    "resume_job",
    "cancel_job",
    "reprioritize_queue",
    "run_replay",
    "run_targeted_tests",
    "create_isolated_candidate_investigation",
})
REPLAY_CASES = frozenset({"error_museum", "prediction_lab"})
TEST_TARGETS = frozenset({"private_store", "private_review", "error_museum", "prediction_lab"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9:._/-]{1,128}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CONFIRM = re.compile(r"\b(confirm|approve|approved|proceed|yes[ ,]+do it|do it|run it)\b", re.I)
_CANCEL = re.compile(r"\b(cancel|reject|decline|never mind|nevermind)\b", re.I)
TERMINAL_FINDINGS = frozenset({
    "RESOLVED", "EXPECTED_BEHAVIOR", "INCONCLUSIVE", "DUPLICATE",
    "ALREADY_FIXED", "FALSE_POSITIVE", "WONT_FIX_WITH_REASON",
})


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _action_id(conversation_id: str, user_text: str, payload: Mapping[str, object]) -> str:
    digest = hashlib.sha256(
        (conversation_id + "\0" + user_text.strip() + "\0" + _json(dict(payload))).encode()
    ).hexdigest()[:24]
    return "action:" + digest


class SafeActionController:
    """Durable proposal ledger plus deterministic bounded action execution."""

    def __init__(self, data_dir: str | Path, *, source_commit: str):
        self.root = Path(data_dir).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError("dedicated persistent data directory required")
        if not _COMMIT.fullmatch(source_commit):
            raise ValueError("exact source commit required for safe actions")
        self.source_commit = source_commit
        self.ai_db = self.root / "ai_ops.sqlite"
        self.state_db = self.root / "supervisor.sqlite"
        if not self.ai_db.is_file():
            raise FileNotFoundError("AI Ops durable state is required")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.state_db, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS supervisor_actions(
                  action_id TEXT PRIMARY KEY,
                  conversation_id TEXT NOT NULL,
                  request_hash TEXT NOT NULL,
                  action TEXT NOT NULL,
                  target_id TEXT NOT NULL,
                  arguments_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN
                    ('PROPOSED','EXECUTING','QUEUED','SUCCEEDED','FAILED','CANCELLED','EXPIRED')),
                  result_json TEXT,
                  created REAL NOT NULL,
                  updated REAL NOT NULL,
                  expires REAL NOT NULL,
                  confirmed REAL
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS supervisor_actions_conversation "
                "ON supervisor_actions(conversation_id,created)"
            )
            interrupted = _json({"class": "INTERRUPTED", "reason": "restart_during_confirmation"})
            db.execute(
                "UPDATE supervisor_actions SET status='FAILED',result_json=?,updated=? "
                "WHERE status='EXECUTING'",
                (interrupted, time.time()),
            )

    @staticmethod
    def _validate_request(payload: Mapping[str, object]) -> dict[str, object]:
        allowed = {"action", "target_id", "replay_case", "test_target", "priority"}
        if set(payload) - allowed:
            raise ValueError("unsupported safe-action field")
        action = str(payload.get("action", ""))
        target_id = str(payload.get("target_id", ""))
        if action not in ACTIONS or not _SAFE_ID.fullmatch(target_id):
            raise ValueError("unsupported safe action or target")
        replay_case = payload.get("replay_case")
        test_target = payload.get("test_target")
        priority = payload.get("priority")
        if action == "run_replay":
            if replay_case not in REPLAY_CASES:
                raise ValueError("approved replay case required")
        elif replay_case is not None:
            raise ValueError("replay_case is only valid for run_replay")
        if action == "run_targeted_tests":
            if test_target not in TEST_TARGETS:
                raise ValueError("approved test target required")
        elif test_target is not None:
            raise ValueError("test_target is only valid for run_targeted_tests")
        if action == "reprioritize_queue":
            if isinstance(priority, bool) or not isinstance(priority, int) or not -100 <= priority <= 100:
                raise ValueError("bounded integer priority required")
        elif priority is not None:
            raise ValueError("priority is only valid for reprioritize_queue")
        return {
            "action": action,
            "target_id": target_id,
            "replay_case": replay_case,
            "test_target": test_target,
            "priority": priority,
        }

    def _conversation_exists(self, conversation_id: str) -> bool:
        if not _SAFE_ID.fullmatch(conversation_id):
            return False
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM supervisor_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone() is not None

    def propose(
        self,
        conversation_id: str,
        user_text: str,
        payload: Mapping[str, object],
        *,
        ttl_seconds: int = 900,
        now: float | None = None,
    ) -> dict[str, object]:
        if not self._conversation_exists(conversation_id):
            raise KeyError("unknown conversation")
        if not 60 <= ttl_seconds <= 3600:
            raise ValueError("bounded confirmation TTL required")
        request = self._validate_request(payload)
        current = time.time() if now is None else now
        action_id = _action_id(conversation_id, user_text, request)
        request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
        encoded = _json(request)
        with self._connect() as db:
            existing = db.execute(
                "SELECT * FROM supervisor_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash or existing["conversation_id"] != conversation_id:
                    raise ValueError("action id collision")
                return self._public(dict(existing))
            db.execute(
                """INSERT INTO supervisor_actions(
                    action_id,conversation_id,request_hash,action,target_id,arguments_json,
                    status,created,updated,expires)
                   VALUES(?,?,?,?,?,?,'PROPOSED',?,?,?)""",
                (
                    action_id,
                    conversation_id,
                    request_hash,
                    request["action"],
                    request["target_id"],
                    encoded,
                    current,
                    current,
                    current + ttl_seconds,
                ),
            )
            row = dict(db.execute(
                "SELECT * FROM supervisor_actions WHERE action_id=?", (action_id,)
            ).fetchone())
        return self._public(row)

    @staticmethod
    def _public(row: Mapping[str, object]) -> dict[str, object]:
        result = {
            "action_id": row["action_id"],
            "conversation_id": row["conversation_id"],
            "action": row["action"],
            "target_id": row["target_id"],
            "status": row["status"],
            "created": row["created"],
            "updated": row["updated"],
            "expires": row["expires"],
            "confirmed": row["confirmed"],
        }
        try:
            arguments = json.loads(str(row.get("arguments_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {}
        for key in ("replay_case", "test_target", "priority"):
            if arguments.get(key) is not None:
                result[key] = arguments[key]
        if row.get("result_json"):
            try:
                result["result"] = json.loads(str(row["result_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                result["result"] = {"class": "UNAVAILABLE"}
        return result

    def _row(self, action_id: str, conversation_id: str) -> dict[str, object]:
        if not _SAFE_ID.fullmatch(action_id) or not _SAFE_ID.fullmatch(conversation_id):
            raise ValueError("invalid action or conversation id")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM supervisor_actions WHERE action_id=? AND conversation_id=?",
                (action_id, conversation_id),
            ).fetchone()
        if row is None:
            raise KeyError("unknown action")
        return dict(row)

    def pending(self, conversation_id: str, *, limit: int = 10) -> list[dict[str, object]]:
        limit = max(1, min(int(limit), 25))
        now = time.time()
        with self._connect() as db:
            db.execute(
                "UPDATE supervisor_actions SET status='EXPIRED',updated=? "
                "WHERE conversation_id=? AND status='PROPOSED' AND expires<=?",
                (now, conversation_id, now),
            )
            rows = db.execute(
                "SELECT * FROM supervisor_actions WHERE conversation_id=? "
                "ORDER BY created DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        return [self._public(dict(row)) for row in rows]

    def cancel_proposal(self, action_id: str, conversation_id: str) -> dict[str, object]:
        row = self._row(action_id, conversation_id)
        if row["status"] in ("CANCELLED", "EXPIRED"):
            return self._public(row)
        if row["status"] != "PROPOSED":
            return self._public(row)
        now = time.time()
        with self._connect() as db:
            db.execute(
                "UPDATE supervisor_actions SET status='CANCELLED',updated=? "
                "WHERE action_id=? AND conversation_id=? AND status='PROPOSED'",
                (now, action_id, conversation_id),
            )
            row = dict(db.execute(
                "SELECT * FROM supervisor_actions WHERE action_id=?", (action_id,)
            ).fetchone())
        return self._public(row)

    def confirm(self, action_id: str, conversation_id: str) -> dict[str, object]:
        row = self._row(action_id, conversation_id)
        now = time.time()
        if row["status"] in ("QUEUED", "SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"):
            return self._public(row)
        if row["status"] == "EXECUTING":
            return self._public(row)
        if float(row["expires"]) <= now:
            with self._connect() as db:
                db.execute(
                    "UPDATE supervisor_actions SET status='EXPIRED',updated=? "
                    "WHERE action_id=? AND status='PROPOSED'",
                    (now, action_id),
                )
            return self._public(self._row(action_id, conversation_id))
        with self._connect() as db:
            changed = db.execute(
                "UPDATE supervisor_actions SET status='EXECUTING',confirmed=?,updated=? "
                "WHERE action_id=? AND conversation_id=? AND status='PROPOSED'",
                (now, now, action_id, conversation_id),
            )
            if changed.rowcount != 1:
                return self._public(self._row(action_id, conversation_id))
        try:
            request = json.loads(str(row["arguments_json"]))
            result, terminal = self._execute(action_id, request)
            if terminal not in ("QUEUED", "SUCCEEDED"):
                raise RuntimeError("invalid safe-action terminal state")
        except Exception as exc:
            result = {"class": "FAILED", "reason": type(exc).__name__}
            terminal = "FAILED"
        encoded = _json(result)
        if len(encoded.encode()) > 8192:
            encoded = _json({"class": "FAILED", "reason": "bounded_result_required"})
            terminal = "FAILED"
        with self._connect() as db:
            db.execute(
                "UPDATE supervisor_actions SET status=?,result_json=?,updated=? "
                "WHERE action_id=? AND status='EXECUTING'",
                (terminal, encoded, time.time(), action_id),
            )
        return self._public(self._row(action_id, conversation_id))

    def _execute(self, action_id: str, request: Mapping[str, object]) -> tuple[dict[str, object], str]:
        action = str(request["action"])
        target = str(request["target_id"])
        store = Store(self.root)
        try:
            if action == "retry_failed_task":
                with store.transaction() as db:
                    row = db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (target,)).fetchone()
                    if row is None:
                        raise KeyError("unknown job")
                    if row["status"] in ("PENDING", "RETRY"):
                        return {"class": "OK", "job_id": target, "status": row["status"]}, "SUCCEEDED"
                    if row["status"] != "FAILED":
                        raise ValueError("only a failed task can be retried")
                    db.execute(
                        "UPDATE ai_ops_jobs SET status='RETRY',lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",
                        (time.time(), target),
                    )
                    store._mark_dr_dirty(db)
                return {"class": "OK", "job_id": target, "status": "RETRY"}, "SUCCEEDED"

            if action in ("pause_job", "resume_job", "cancel_job", "reprioritize_queue"):
                return self._job_control(store, action, target, request)

            if action == "request_reviewer":
                return self._request_reviewer(store, target)

            if action in ("start_investigation", "create_isolated_candidate_investigation"):
                return self._start_investigation(store, action_id, target, candidate=(action.startswith("create_")))

            if action == "run_replay":
                return self._queue_tool(
                    store, action_id, target, "run_replay",
                    {"case": str(request["replay_case"])},
                )

            if action == "run_targeted_tests":
                return self._queue_tool(
                    store, action_id, target, "run_test",
                    {"target": str(request["test_target"]), "candidate": None},
                )

            raise ValueError("safe action is not implemented")
        finally:
            store.close()

    @staticmethod
    def _job_control(
        store: Store, action: str, job_id: str, request: Mapping[str, object]
    ) -> tuple[dict[str, object], str]:
        now = time.time()
        with store.transaction() as db:
            row = db.execute("SELECT status,priority FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError("unknown job")
            status = str(row["status"])
            if action == "pause_job":
                if status == "PAUSED":
                    return {"class": "OK", "job_id": job_id, "status": "PAUSED"}, "SUCCEEDED"
                if status not in ("PENDING", "RETRY", "RUNNING"):
                    raise ValueError("job cannot be paused from current state")
                db.execute(
                    "UPDATE ai_ops_jobs SET status='PAUSED',lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",
                    (now, job_id),
                )
                db.execute(
                    "UPDATE ai_ops_steps SET status=CASE WHEN status='RUNNING' THEN 'RETRY' ELSE status END,"
                    "lease_owner=NULL,lease_until=NULL WHERE job_id=? AND status!='COMPLETE'",
                    (job_id,),
                )
                db.execute(
                    "UPDATE ai_ops_attempts SET ended=?,result='PAUSED' "
                    "WHERE job_id=? AND ended IS NULL",
                    (now, job_id),
                )
                cancelled_tools = db.execute(
                    "UPDATE ai_ops_tool_operations SET status='CANCELLED',result_class='PAUSED_BY_OWNER',"
                    "finished=?,lease_owner=NULL,lease_until=NULL "
                    "WHERE job_id=? AND status IN ('QUEUED','RUNNING')",
                    (now, job_id),
                ).rowcount
                store._mark_dr_dirty(db)
                return {
                    "class": "OK", "job_id": job_id, "status": "PAUSED",
                    "cancelled_tool_operations": cancelled_tools,
                }, "SUCCEEDED"
            if action == "resume_job":
                if status in ("PENDING", "RETRY"):
                    return {"class": "OK", "job_id": job_id, "status": status}, "SUCCEEDED"
                if status != "PAUSED":
                    raise ValueError("only a paused job can be resumed")
                db.execute(
                    "UPDATE ai_ops_jobs SET status='RETRY',updated=? WHERE id=?", (now, job_id)
                )
                store._mark_dr_dirty(db)
                return {"class": "OK", "job_id": job_id, "status": "RETRY"}, "SUCCEEDED"
            if action == "cancel_job":
                if status == "CANCELLED":
                    return {"class": "OK", "job_id": job_id, "status": "CANCELLED"}, "SUCCEEDED"
                if status == "COMPLETE":
                    raise ValueError("completed job cannot be cancelled")
                db.execute(
                    "UPDATE ai_ops_jobs SET status='CANCELLED',lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?",
                    (now, job_id),
                )
                db.execute(
                    "UPDATE ai_ops_steps SET status=CASE WHEN status='RUNNING' THEN 'RETRY' ELSE status END,"
                    "lease_owner=NULL,lease_until=NULL WHERE job_id=? AND status!='COMPLETE'",
                    (job_id,),
                )
                db.execute(
                    "UPDATE ai_ops_attempts SET ended=?,result='CANCELLED' "
                    "WHERE job_id=? AND ended IS NULL",
                    (now, job_id),
                )
                cancelled_tools = db.execute(
                    "UPDATE ai_ops_tool_operations SET status='CANCELLED',result_class='CANCELLED_BY_OWNER',"
                    "finished=?,lease_owner=NULL,lease_until=NULL "
                    "WHERE job_id=? AND status IN ('QUEUED','RUNNING')",
                    (now, job_id),
                ).rowcount
                store._mark_dr_dirty(db)
                return {
                    "class": "OK", "job_id": job_id, "status": "CANCELLED",
                    "cancelled_tool_operations": cancelled_tools,
                }, "SUCCEEDED"
            if action == "reprioritize_queue":
                priority = int(request["priority"])
                if status in ("COMPLETE", "CANCELLED"):
                    raise ValueError("terminal job cannot be reprioritized")
                db.execute(
                    "UPDATE ai_ops_jobs SET priority=?,updated=? WHERE id=?",
                    (priority, now, job_id),
                )
                store._mark_dr_dirty(db)
                return {
                    "class": "OK", "job_id": job_id, "status": status, "priority": priority,
                }, "SUCCEEDED"
        raise ValueError("unknown job control action")

    def _request_reviewer(self, store: Store, finding_id: str) -> tuple[dict[str, object], str]:
        with store.transaction() as db:
            finding = db.execute(
                "SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding_id,)
            ).fetchone()
            if finding is None:
                raise KeyError("unknown finding")
            if finding["status"] in TERMINAL_FINDINGS:
                raise ValueError("terminal finding cannot be re-reviewed")
            case = db.execute(
                """SELECT c.packet_id,c.state,c.batch_id FROM ai_ops_cases c
                   JOIN ai_ops_finding_packets fp ON fp.packet_id=c.packet_id
                   WHERE fp.finding_id=? ORDER BY fp.created DESC LIMIT 1""",
                (finding_id,),
            ).fetchone()
            if case is None:
                raise ValueError("finding has no reviewable evidence packet")
            if case["batch_id"] is not None:
                return {
                    "class": "OK", "finding_id": finding_id, "packet_id": case["packet_id"],
                    "status": "ALREADY_QUEUED",
                }, "QUEUED"
            db.execute(
                """UPDATE ai_ops_cases SET state='REVIEWING',pending_role='independent_review',
                   escalation_reason='owner_requested_review',updated=? WHERE packet_id=?""",
                (time.time(), case["packet_id"]),
            )
            store._mark_dr_dirty(db)
        queued = queue_review_batches(store, limit=1)
        if not queued:
            raise RuntimeError("review request was not queued")
        return {
            "class": "OK", "finding_id": finding_id, "packet_id": case["packet_id"],
            "review_job_id": queued[0], "status": "QUEUED",
        }, "QUEUED"

    def _start_investigation(
        self, store: Store, action_id: str, finding_id: str, *, candidate: bool
    ) -> tuple[dict[str, object], str]:
        row = store.db.execute(
            "SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding_id,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown finding")
        if row["status"] not in ACTIVE_FINDINGS:
            raise ValueError("finding is not active")
        suffix = action_id.split(":", 1)[1][:16]
        job_id = ("task:candidate:" if candidate else "task:investigate:") + suffix
        store.enqueue(job_id, priority=35 if candidate else 30)
        now = time.time()
        with store.transaction() as db:
            db.execute(
                "UPDATE ai_ops_findings SET status='INVESTIGATING',updated=? WHERE finding_id=?",
                (now, finding_id),
            )
            store._mark_dr_dirty(db)
        operation_ids = []
        op1 = "op:" + suffix + ":status"
        store.queue_tool(
            op1, job_id, "repository_status", {}, self.source_commit, finding_id=finding_id
        )
        operation_ids.append(op1)
        if candidate:
            op2 = "op:" + suffix + ":commit"
            store.queue_tool(
                op2, job_id, "inspect_commit", {}, self.source_commit, finding_id=finding_id
            )
            operation_ids.append(op2)
        return {
            "class": "OK", "finding_id": finding_id, "job_id": job_id,
            "operation_ids": operation_ids, "status": "QUEUED_FOR_ISOLATED_RUNNER",
            "source_commit": self.source_commit,
        }, "QUEUED"

    def _queue_tool(
        self,
        store: Store,
        action_id: str,
        job_id: str,
        tool: str,
        arguments: dict[str, object],
    ) -> tuple[dict[str, object], str]:
        job = store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            raise KeyError("unknown job")
        if job["status"] in ("COMPLETE", "CANCELLED"):
            raise ValueError("terminal job cannot accept an engineering operation")
        suffix = action_id.split(":", 1)[1][:16]
        kind = "replay" if tool == "run_replay" else "test"
        operation_id = f"op:{suffix}:{kind}"
        row = store.queue_tool(
            operation_id, job_id, tool, arguments, self.source_commit
        )
        return {
            "class": "OK",
            "job_id": job_id,
            "operation_id": operation_id,
            "operation_status": row["status"],
            "status": "QUEUED_FOR_ISOLATED_RUNNER",
            "source_commit": self.source_commit,
        }, "QUEUED"


class ActionSupervisorBackend(SupervisorBackend):
    """Read tools plus proposal/confirmation tools bound to the current chat request."""

    ACTION_TOOL_NAMES = ("propose_safe_action", "confirm_safe_action", "cancel_safe_action")

    def __init__(
        self,
        db_path: str | Path,
        *,
        source_commit: str | None = None,
    ):
        super().__init__(db_path)
        source = source_commit if source_commit is not None else os.environ.get("AI_OPS_SOURCE_COMMIT", "")
        self.actions: SafeActionController | None = None
        if _COMMIT.fullmatch(source or ""):
            self.actions = SafeActionController(Path(db_path).resolve().parent, source_commit=source)
        self._context = threading.local()

    @contextmanager
    def bind(self, conversation_id: str, user_text: str):
        previous = getattr(self._context, "value", None)
        self._context.value = (conversation_id, user_text)
        try:
            yield
        finally:
            self._context.value = previous

    def _bound(self) -> tuple[str, str] | None:
        value = getattr(self._context, "value", None)
        return value if isinstance(value, tuple) and len(value) == 2 else None

    def call(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        if name not in self.ACTION_TOOL_NAMES:
            return super().call(name, arguments)
        if self.actions is None:
            return {"available": False, "reason": "safe_actions_not_configured"}
        bound = self._bound()
        if bound is None:
            return {"available": False, "reason": "no_active_authenticated_chat_context"}
        conversation_id, user_text = bound
        args = dict(arguments or {})
        try:
            if name == "propose_safe_action":
                proposal = self.actions.propose(conversation_id, user_text, args)
                return {
                    "available": True,
                    "confirmation_required": True,
                    "instruction": "Ask the owner to confirm using the exact action_id.",
                    "action": proposal,
                }
            action_id = str(args.get("action_id", ""))
            if not _SAFE_ID.fullmatch(action_id):
                raise ValueError("valid action_id required")
            if action_id not in user_text:
                return {
                    "available": False, "reason": "exact_action_id_not_in_owner_message",
                    "action_id": action_id,
                }
            if name == "confirm_safe_action":
                if not _CONFIRM.search(user_text):
                    return {
                        "available": False, "reason": "explicit_owner_confirmation_missing",
                        "action_id": action_id,
                    }
                return {"available": True, "action": self.actions.confirm(action_id, conversation_id)}
            if not _CANCEL.search(user_text):
                return {
                    "available": False, "reason": "explicit_owner_cancellation_missing",
                    "action_id": action_id,
                }
            return {"available": True, "action": self.actions.cancel_proposal(action_id, conversation_id)}
        except (KeyError, ValueError, sqlite3.Error, QueueFull):
            return {"available": False, "reason": "safe_action_rejected", "tool": name}

    def tool_definitions(self) -> list[dict[str, object]]:
        base_names = set(SupervisorBackend.TOOL_NAMES)
        definitions = [
            item for item in super().tool_definitions()
            if item.get("function", {}).get("name") in base_names
        ]
        if self.actions is None:
            return definitions
        definitions.append({
            "type": "function",
            "function": {
                "name": "propose_safe_action",
                "description": (
                    "Propose one bounded owner action. This never confirms itself and never "
                    "merges, deploys, changes secrets/config, or changes flight decisions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": sorted(ACTIONS)},
                        "target_id": {"type": "string", "minLength": 1, "maxLength": 128},
                        "replay_case": {"type": "string", "enum": sorted(REPLAY_CASES)},
                        "test_target": {"type": "string", "enum": sorted(TEST_TARGETS)},
                        "priority": {"type": "integer", "minimum": -100, "maximum": 100},
                    },
                    "required": ["action", "target_id"],
                    "additionalProperties": False,
                },
            },
        })
        for name, description in (
            ("confirm_safe_action", "Confirm one previously proposed action after explicit owner approval."),
            ("cancel_safe_action", "Cancel one unconfirmed action after explicit owner cancellation."),
        ):
            definitions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action_id": {"type": "string", "pattern": "^action:[0-9a-f]{24}$"},
                        },
                        "required": ["action_id"],
                        "additionalProperties": False,
                    },
                },
            })
        return definitions


class ActionSupervisorEngine(SupervisorEngine):
    """Supervisor with confirmation-gated safe action tools, never production mutation."""

    @staticmethod
    def _system_prompt(snapshot: Mapping[str, object]) -> str:
        base = SupervisorEngine._system_prompt(snapshot)
        return base + (
            "\nSAFE_ACTIONS: When the owner explicitly asks to operate AI Ops, use "
            "propose_safe_action for only the allowlisted analysis actions. A proposal does "
            "not execute anything. Report its exact action_id and ask for explicit confirmation. "
            "Only call confirm_safe_action when the current owner message itself contains the "
            "exact action_id and explicitly confirms/approves/proceeds. Only call "
            "cancel_safe_action when the current owner message contains the exact action_id and "
            "explicitly cancels/rejects it. Never claim QUEUED_FOR_ISOLATED_RUNNER means the "
            "replay/test already ran. There is no merge/deploy/secret/config/flight-decision tool."
        )

    def chat(self, conversation_id: str, user_text: str) -> dict[str, object]:
        if isinstance(self.backend, ActionSupervisorBackend):
            with self.backend.bind(conversation_id, user_text):
                result = super().chat(conversation_id, user_text)
            if self.backend.actions is not None:
                result["safe_actions"] = self.backend.actions.pending(conversation_id)
            return result
        return super().chat(conversation_id, user_text)


def action_aware_extend(original_extend):
    """Wrap the existing authenticated web handler only to correct authority metadata."""
    def extend(base_handler, supervisor):
        handler = original_extend(base_handler, supervisor)

        class ActionAwareHandler(handler):
            def do_GET(self):
                from urllib.parse import urlparse

                if (
                    urlparse(self.path).path == "/api/supervisor/status"
                    and isinstance(supervisor, ActionSupervisorEngine)
                    and isinstance(supervisor.backend, ActionSupervisorBackend)
                    and supervisor.backend.actions is not None
                ):
                    session = self._supervisor_session()
                    if session is None:
                        return
                    providers = []
                    for slot in supervisor.slots:
                        if slot.provider not in providers:
                            providers.append(slot.provider)
                    self._json(200, {
                        "enabled": True,
                        "primary_provider": (
                            "cloudflare" if "cloudflare" in providers
                            else (providers[0] if providers else "unavailable")
                        ),
                        "providers": providers,
                        "model": supervisor.cloudflare_model,
                        "authority": "confirmation_gated_safe_actions",
                        "production_mutation": False,
                        "flight_decision_authority": False,
                    })
                    return
                super().do_GET()

        return ActionAwareHandler

    return extend
