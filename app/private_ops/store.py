"""Durable, bounded job and step state for the separate Private Operations service.

The caller must supply a directory on a dedicated persistent volume. There is
deliberately no fallback to an ephemeral path or the live Prediction Lab volume.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 7
STATUSES = frozenset({"PENDING", "RUNNING", "COMPLETE", "FAILED", "RETRY"})


class QueueFull(RuntimeError):
    """New optional work was rejected to bound durable storage."""


class Store:
    def __init__(self, data_dir: str | Path, *, max_pending: int = 512):
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.root = Path(data_dir).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError("Dedicated persistent data directory is required")
        self.path = self.root / "ai_ops.sqlite"
        self.max_pending = max_pending
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _migrate(self) -> None:
        with self.transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("AI Ops database schema is newer than this application")
            if version == 0:
                schema = """
                    CREATE TABLE ai_ops_jobs (
                      id TEXT PRIMARY KEY, status TEXT NOT NULL, priority INTEGER NOT NULL,
                      created REAL NOT NULL, updated REAL NOT NULL,
                      lease_owner TEXT, lease_until REAL, CHECK (length(id) BETWEEN 1 AND 128)
                    );
                    CREATE TABLE ai_ops_steps (
                      job_id TEXT NOT NULL REFERENCES ai_ops_jobs(id), step_id TEXT NOT NULL,
                      status TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT,
                      output_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                      lease_owner TEXT, lease_until REAL,
                      PRIMARY KEY(job_id, step_id)
                    );
                    CREATE TABLE ai_ops_attempts (
                      id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, step_id TEXT NOT NULL,
                      worker TEXT NOT NULL, started REAL NOT NULL, ended REAL,
                      result TEXT, FOREIGN KEY(job_id,step_id) REFERENCES ai_ops_steps(job_id,step_id)
                    );
                    CREATE TABLE ai_ops_scheduler (
                      name TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, updated REAL NOT NULL
                    );
                    CREATE INDEX ai_ops_jobs_claim ON ai_ops_jobs(status,priority DESC,created);
                """
                for statement in schema.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("PRAGMA user_version=1")
                version = 1
            if version == 1:
                db.execute("""CREATE TABLE ai_ops_dr_sync (
                    destination TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
                    synced_revision INTEGER NOT NULL DEFAULT 0,
                    last_attempt REAL, last_verified REAL, last_hash TEXT,
                    next_attempt REAL NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_until REAL,
                    CHECK (synced_revision <= revision)
                )""")
                db.execute("""INSERT INTO ai_ops_dr_sync(destination) VALUES('github'),('dropbox')""")
                if db.execute("SELECT 1 FROM ai_ops_jobs LIMIT 1").fetchone() or db.execute("SELECT 1 FROM ai_ops_scheduler LIMIT 1").fetchone():
                    self._mark_dr_dirty(db)
                db.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                db.execute("""CREATE TABLE ai_ops_provider_health (
                    slot TEXT PRIMARY KEY, provider TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
                    last_status TEXT NOT NULL DEFAULT 'unverified', updated REAL NOT NULL DEFAULT 0
                )""")
                db.execute("""CREATE TABLE ai_ops_provider_circuit (
                    provider TEXT PRIMARY KEY, consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    open_until REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0
                )""")
                db.execute("PRAGMA user_version=3")
                version = 3
            if version == 3:
                db.execute("""CREATE TABLE ai_ops_evidence (
                    packet_id TEXT PRIMARY KEY, window_start INTEGER NOT NULL,
                    case_ref TEXT NOT NULL, kind TEXT NOT NULL,
                    packet_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                    created REAL NOT NULL
                )""")
                db.execute("CREATE INDEX ai_ops_evidence_window ON ai_ops_evidence(window_start)")
                db.execute("PRAGMA user_version=4")
                version = 4
            if version == 4:
                # Phase 5: durable AI-assisted analysis state. Model text is
                # bounded/sanitized; prompts and raw provider bodies are never stored.
                db.execute("""CREATE TABLE ai_ops_batches (
                    batch_id TEXT PRIMARY KEY, role TEXT NOT NULL, packet_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    CHECK (length(batch_id) BETWEEN 1 AND 128)
                )""")
                db.execute("""CREATE TABLE ai_ops_cases (
                    packet_id TEXT PRIMARY KEY REFERENCES ai_ops_evidence(packet_id),
                    state TEXT NOT NULL DEFAULT 'PENDING_AI', pending_role TEXT NOT NULL DEFAULT 'triage', classification TEXT,
                    severity TEXT, validation_status TEXT, finding_id TEXT,
                    batch_id TEXT REFERENCES ai_ops_batches(batch_id),
                    retry_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, created REAL NOT NULL, updated REAL NOT NULL
                )""")
                db.execute("CREATE INDEX ai_ops_cases_state ON ai_ops_cases(state,updated)")
                db.execute("""CREATE TABLE ai_ops_usage (
                    id INTEGER PRIMARY KEY, batch_id TEXT, packet_id TEXT, task_role TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL, key_slot TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL, success INTEGER NOT NULL,
                    failure_kind TEXT, malformed INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                )""")
                db.execute("CREATE INDEX ai_ops_usage_provider ON ai_ops_usage(provider,key_slot,created)")
                db.execute("""CREATE TABLE ai_ops_reviews (
                    review_id TEXT PRIMARY KEY, packet_id TEXT NOT NULL REFERENCES ai_ops_evidence(packet_id),
                    role TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, key_slot TEXT NOT NULL,
                    classification TEXT NOT NULL, severity TEXT NOT NULL,
                    validation_status TEXT NOT NULL, result_json TEXT NOT NULL,
                    agreement TEXT, disposition TEXT, created REAL NOT NULL
                )""")
                db.execute("CREATE INDEX ai_ops_reviews_packet ON ai_ops_reviews(packet_id,created)")
                db.execute("""CREATE TABLE ai_ops_findings (
                    finding_id TEXT PRIMARY KEY, signature TEXT NOT NULL, occurrence INTEGER NOT NULL,
                    status TEXT NOT NULL, classification TEXT NOT NULL, severity TEXT NOT NULL,
                    subsystem TEXT NOT NULL, case_ref TEXT NOT NULL,
                    previous_finding_id TEXT REFERENCES ai_ops_findings(finding_id),
                    fix_commit TEXT, fix_version TEXT, resolution_json TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(signature, occurrence)
                )""")
                db.execute("CREATE INDEX ai_ops_findings_active ON ai_ops_findings(status,updated)")
                db.execute("""CREATE TABLE ai_ops_finding_packets (
                    finding_id TEXT NOT NULL REFERENCES ai_ops_findings(finding_id),
                    packet_id TEXT NOT NULL REFERENCES ai_ops_evidence(packet_id),
                    created REAL NOT NULL, PRIMARY KEY(finding_id,packet_id)
                )""")
                db.execute("PRAGMA user_version=5")
                version = 5
            if version == 5:
                db.execute("ALTER TABLE ai_ops_reviews ADD COLUMN independence TEXT NOT NULL DEFAULT 'NOT_APPLICABLE'")
                db.execute("ALTER TABLE ai_ops_cases ADD COLUMN escalation_reason TEXT NOT NULL DEFAULT ''")
                db.execute("""CREATE TABLE ai_ops_feedback (
                    finding_id TEXT PRIMARY KEY REFERENCES ai_ops_findings(finding_id),
                    verdict TEXT NOT NULL CHECK (verdict IN
                        ('USEFUL','NOT_USEFUL','FALSE_POSITIVE','NEEDS_MORE_INVESTIGATION',
                         'ALREADY_KNOWN','CORRECTLY_IDENTIFIED_PROBLEM','INSUFFICIENT_EVIDENCE','WRONG_CONCLUSION')),
                    updated REAL NOT NULL
                )""")
                db.execute("PRAGMA user_version=6")
                version = 6
            if version == 6:
                db.execute("""CREATE TABLE ai_ops_tool_operations (
                    operation_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES ai_ops_jobs(id),
                    finding_id TEXT REFERENCES ai_ops_findings(finding_id),
                    tool TEXT NOT NULL, arguments_json TEXT NOT NULL, input_hash TEXT NOT NULL,
                    source_commit TEXT NOT NULL, sandbox_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('QUEUED','RUNNING','SUCCEEDED','FAILED','TIMED_OUT','CANCELLED')),
                    lease_owner TEXT, lease_until REAL, started REAL, finished REAL,
                    result_class TEXT, result_json TEXT, output_hash TEXT,
                    artifact_hash TEXT, retry_of TEXT REFERENCES ai_ops_tool_operations(operation_id),
                    created REAL NOT NULL
                )""")
                db.execute("CREATE INDEX ai_ops_tool_job ON ai_ops_tool_operations(job_id,created)")
                db.execute("PRAGMA user_version=7")

    @staticmethod
    def _mark_dr_dirty(db: sqlite3.Connection) -> None:
        db.execute("""UPDATE ai_ops_dr_sync SET revision=revision+1,
            next_attempt=CASE WHEN next_attempt=0 THEN ? ELSE next_attempt END
            WHERE destination='github'""", (time.time() + 60,))

    @staticmethod
    def digest(value: object) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    def enqueue(self, job_id: str, *, priority: int = 0) -> bool:
        if not job_id or len(job_id) > 128:
            raise ValueError("invalid job id")
        now = time.time()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone():
                return False
            count = db.execute("SELECT count(*) FROM ai_ops_jobs WHERE status IN ('PENDING','RETRY','RUNNING')").fetchone()[0]
            if count >= self.max_pending:
                raise QueueFull("Private Operations queue capacity reached")
            db.execute("INSERT INTO ai_ops_jobs(id,status,priority,created,updated) VALUES(?,'PENDING',?,?,?)", (job_id, priority, now, now))
            self._mark_dr_dirty(db)
        return True

    def queue_tool(self, operation_id: str, job_id: str, tool: str, arguments: dict[str, object],
                   source_commit: str, *, finding_id: str | None = None,
                   retry_of: str | None = None) -> dict[str, object]:
        """Register one immutable, bounded operation under an existing durable job."""
        import re
        if (not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", operation_id)
                or not re.fullmatch(r"[0-9a-f]{40}", source_commit)):
            raise ValueError("invalid operation id or source commit")
        encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 4096:
            raise ValueError("tool arguments too large")
        digest = self.digest([tool, arguments, source_commit, finding_id])
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if existing:
                if existing["input_hash"] != digest or existing["job_id"] != job_id:
                    raise ValueError("operation id reused with different input")
                return dict(existing)
            if not db.execute("SELECT 1 FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone():
                raise ValueError("tool operation requires an existing job")
            if db.execute("SELECT count(*) FROM ai_ops_tool_operations WHERE status IN ('QUEUED','RUNNING')").fetchone()[0] >= 16:
                raise QueueFull("too many pending engineering tool operations")
            now = time.time()
            db.execute("""INSERT INTO ai_ops_tool_operations
                (operation_id,job_id,finding_id,tool,arguments_json,input_hash,source_commit,sandbox_id,status,retry_of,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (operation_id, job_id, finding_id, tool, encoded, digest, source_commit,
                 'sandbox-' + operation_id, 'QUEUED', retry_of, now))
            self._mark_dr_dirty(db)
            return dict(db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def claim_tool(self, operation_id: str, worker: str, *, lease_seconds: int = 120) -> dict[str, object] | None:
        if not worker or not 1 <= lease_seconds <= 300:
            raise ValueError("invalid tool worker or lease")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError("unknown tool operation")
            if row["status"] in ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"):
                return None
            now = time.time()
            if row["status"] == 'RUNNING' and row["lease_until"] > now:
                raise PermissionError("tool operation held by another worker")
            # An expired RUNNING operation is uncertain: the process may have
            # finished its command before dying. Never silently rerun it.
            if row["status"] == 'RUNNING':
                db.execute("""UPDATE ai_ops_tool_operations SET status='FAILED',result_class='INTERRUPTED',
                    finished=?,lease_owner=NULL,lease_until=NULL WHERE operation_id=?""", (now, operation_id))
                self._mark_dr_dirty(db)
                return None
            if db.execute("SELECT count(*) FROM ai_ops_tool_operations WHERE status='RUNNING'").fetchone()[0] >= 1:
                raise QueueFull("engineering sandbox concurrency limit reached")
            db.execute("""UPDATE ai_ops_tool_operations SET status='RUNNING',lease_owner=?,lease_until=?,started=?
                WHERE operation_id=?""", (worker, now + lease_seconds, now, operation_id))
            self._mark_dr_dirty(db)
            return dict(db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def finish_tool(self, operation_id: str, worker: str, status: str, result: dict[str, object],
                    *, artifact_hash: str | None = None) -> dict[str, object]:
        if status not in ('SUCCEEDED', 'FAILED', 'TIMED_OUT', 'CANCELLED'):
            raise ValueError("invalid tool terminal status")
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 16384:
            raise ValueError("tool result exceeds durable size limit")
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        if artifact_hash is not None and (len(artifact_hash) != 64 or any(c not in '0123456789abcdef' for c in artifact_hash)):
            raise ValueError("invalid artifact hash")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone()
            if not row or row['status'] != 'RUNNING' or row['lease_owner'] != worker or row['lease_until'] <= time.time():
                raise PermissionError("worker no longer owns tool operation")
            db.execute("""UPDATE ai_ops_tool_operations SET status=?,result_class=?,result_json=?,output_hash=?,
                artifact_hash=?,finished=?,lease_owner=NULL,lease_until=NULL WHERE operation_id=?""",
                (status, str(result.get('class', status))[:64], encoded, digest, artifact_hash, time.time(), operation_id))
            self._mark_dr_dirty(db)
            return dict(db.execute("SELECT * FROM ai_ops_tool_operations WHERE operation_id=?", (operation_id,)).fetchone())

    def claim(self, worker: str, *, lease_seconds: int = 300) -> str | None:
        if not worker or lease_seconds < 1:
            raise ValueError("worker and positive lease required")
        now = time.time()
        with self.transaction() as db:
            row = db.execute("""SELECT id FROM ai_ops_jobs
                WHERE status IN ('PENDING','RETRY') OR (status='RUNNING' AND lease_until<=?)
                ORDER BY priority DESC, created LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE ai_ops_jobs SET status='RUNNING',lease_owner=?,lease_until=?,updated=? WHERE id=?",
                       (worker, now + lease_seconds, now, row["id"]))
            return row["id"]

    def heartbeat(self, job_id: str, worker: str, *, lease_seconds: int = 300) -> bool:
        if lease_seconds < 1:
            raise ValueError("positive lease required")
        now = time.time()
        with self.transaction() as db:
            result = db.execute("""UPDATE ai_ops_jobs SET lease_until=?,updated=?
                WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_until>?""",
                (now + lease_seconds, now, job_id, worker, now))
            return result.rowcount == 1

    def begin_step(self, job_id: str, step_id: str, worker: str, input_data: object) -> bool:
        """Return False when this exact step has already committed its output."""
        if not step_id or len(step_id) > 128:
            raise ValueError("invalid step id")
        input_hash = self.digest(input_data)
        now = time.time()
        with self.transaction() as db:
            job = db.execute("SELECT * FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "RUNNING" or job["lease_owner"] != worker or job["lease_until"] <= now:
                raise PermissionError("worker does not hold the job lease")
            step = db.execute("SELECT * FROM ai_ops_steps WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
            if step:
                if step["input_hash"] != input_hash:
                    raise ValueError("step identifier reused with different input")
                if step["status"] == "COMPLETE":
                    return False
                if step["status"] == "RUNNING" and step["lease_until"] and step["lease_until"] > now and step["lease_owner"] != worker:
                    raise PermissionError("step is owned by another worker")
                db.execute("""UPDATE ai_ops_steps SET status='RUNNING',attempts=attempts+1,
                    lease_owner=?,lease_until=? WHERE job_id=? AND step_id=?""",
                    (worker, job["lease_until"], job_id, step_id))
            else:
                db.execute("""INSERT INTO ai_ops_steps(job_id,step_id,status,input_hash,attempts,lease_owner,lease_until)
                    VALUES(?,?,'RUNNING',?,1,?,?)""", (job_id, step_id, input_hash, worker, job["lease_until"]))
            db.execute("INSERT INTO ai_ops_attempts(job_id,step_id,worker,started) VALUES(?,?,?,?)", (job_id, step_id, worker, now))
            self._mark_dr_dirty(db)
            return True

    def complete_step(self, job_id: str, step_id: str, worker: str, output: object) -> str:
        encoded = json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise ValueError("step output exceeds durable size limit")
        digest = self.digest(output)
        now = time.time()
        with self.transaction() as db:
            step = db.execute("SELECT * FROM ai_ops_steps WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
            job = db.execute("SELECT * FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
            if step and step["status"] == "COMPLETE":
                if step["output_hash"] != digest:
                    raise ValueError("completed step output cannot be changed")
                return digest
            if not step or not job or job["status"] != "RUNNING" or job["lease_owner"] != worker or job["lease_until"] <= now or step["lease_owner"] != worker:
                raise PermissionError("worker no longer owns this step")
            db.execute("""UPDATE ai_ops_steps SET status='COMPLETE',output_hash=?,output_json=?,lease_owner=NULL,lease_until=NULL
                WHERE job_id=? AND step_id=?""", (digest, encoded, job_id, step_id))
            db.execute("""UPDATE ai_ops_attempts SET ended=?,result='COMPLETE' WHERE id=(
                SELECT id FROM ai_ops_attempts WHERE job_id=? AND step_id=? AND worker=? AND ended IS NULL ORDER BY id DESC LIMIT 1)""",
                (now, job_id, step_id, worker))
            self._mark_dr_dirty(db)
        return digest

    def step_output(self, job_id: str, step_id: str) -> object | None:
        row = self.db.execute("SELECT status,output_json FROM ai_ops_steps WHERE job_id=? AND step_id=?", (job_id, step_id)).fetchone()
        if row is None or row["status"] != "COMPLETE" or row["output_json"] is None:
            return None
        return json.loads(row["output_json"])

    def retry_job(self, job_id: str, worker: str, *, result: str = "RETRY") -> None:
        """Release a lease without losing incomplete durable work."""
        now = time.time()
        with self.transaction() as db:
            job = db.execute("SELECT * FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["status"] != "RUNNING" or job["lease_owner"] != worker:
                raise PermissionError("worker does not own job")
            db.execute("""UPDATE ai_ops_steps SET status='RETRY',lease_owner=NULL,lease_until=NULL
                WHERE job_id=? AND status='RUNNING' AND lease_owner=?""", (job_id, worker))
            db.execute("""UPDATE ai_ops_attempts SET ended=?,result=?
                WHERE job_id=? AND worker=? AND ended IS NULL""", (now, result[:40], job_id, worker))
            db.execute("UPDATE ai_ops_jobs SET status='RETRY',lease_owner=NULL,lease_until=NULL,updated=? WHERE id=?", (now, job_id))
            self._mark_dr_dirty(db)

    def finish_job(self, job_id: str, worker: str) -> None:
        now = time.time()
        with self.transaction() as db:
            pending = db.execute("SELECT count(*) FROM ai_ops_steps WHERE job_id=? AND status!='COMPLETE'", (job_id,)).fetchone()[0]
            if pending:
                raise RuntimeError("job has incomplete steps")
            result = db.execute("""UPDATE ai_ops_jobs SET status='COMPLETE',lease_owner=NULL,lease_until=NULL,updated=?
                WHERE id=? AND lease_owner=? AND lease_until>? AND status='RUNNING'""", (now, job_id, worker, now))
            if result.rowcount != 1:
                raise PermissionError("worker no longer owns job")
            self._mark_dr_dirty(db)

    def checkpoint(self, name: str, value: str) -> None:
        with self.transaction() as db:
            db.execute("""INSERT INTO ai_ops_scheduler(name,checkpoint,updated) VALUES(?,?,?)
                ON CONFLICT(name) DO UPDATE SET checkpoint=excluded.checkpoint,updated=excluded.updated""",
                (name, value, time.time()))
            self._mark_dr_dirty(db)

    def health(self) -> dict[str, object]:
        tables = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pending_ai = self.db.execute("SELECT count(*) FROM ai_ops_cases WHERE state='PENDING_AI'").fetchone()[0] if "ai_ops_cases" in tables else 0
        active_findings = self.db.execute("""SELECT count(*) FROM ai_ops_findings WHERE status NOT IN
            ('RESOLVED','EXPECTED_BEHAVIOR','INCONCLUSIVE','DUPLICATE','ALREADY_FIXED','FALSE_POSITIVE','WONT_FIX_WITH_REASON')""").fetchone()[0] if "ai_ops_findings" in tables else 0
        return {"schema": self.db.execute("PRAGMA user_version").fetchone()[0],
                "integrity": self.db.execute("PRAGMA quick_check").fetchone()[0],
                "pending": self.db.execute("SELECT count(*) FROM ai_ops_jobs WHERE status IN ('PENDING','RETRY')").fetchone()[0],
                "pending_ai": pending_ai, "active_findings": active_findings}

    def close(self) -> None:
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
