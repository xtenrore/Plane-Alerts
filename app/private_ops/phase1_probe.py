"""Bounded one-time Railway persistence/recovery acceptance probe.

Only the dedicated Phase 1 image invokes this; it uses synthetic payloads and
does not claim or alter real audit work. The completed marker is durable.
"""
from __future__ import annotations

import sqlite3
import time

from app.private_ops.store import QueueFull, Store

JOB = "phase1:railway-volume-restart-probe"


def run(store: Store) -> str:
    stage_row = store.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='phase1_probe'").fetchone()
    stage = stage_row[0] if stage_row else "initial"
    if stage == "initial":
        assert store.enqueue(JOB)
        assert store.claim("probe-first", lease_seconds=1) == JOB
        assert store.begin_step(JOB, "completed", "probe-first", {"fixture": 1})
        store.complete_step(JOB, "completed", "probe-first", {"ok": True})
        assert store.begin_step(JOB, "interrupted", "probe-first", {"fixture": 2})
        # A provider/worker crash after beginning a step commits no partial result.
        try:
            raise RuntimeError("synthetic worker interruption")
        except RuntimeError:
            pass
        store.checkpoint("phase1_probe", "awaiting_restart")
        return "PHASE1_INITIALIZED_AWAITING_RESTART"
    if stage == "awaiting_restart":
        job = store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()
        assert job and job[0] == "RUNNING"
        first = store.db.execute("SELECT status,output_hash FROM ai_ops_steps WHERE job_id=? AND step_id='completed'", (JOB,)).fetchone()
        assert first and first[0] == "COMPLETE" and first[1] == Store.digest({"ok": True})
        assert store.claim("probe-second") == JOB
        assert not store.begin_step(JOB, "completed", "probe-second", {"fixture": 1})
        assert store.begin_step(JOB, "interrupted", "probe-second", {"fixture": 2})
        store.complete_step(JOB, "interrupted", "probe-second", {"recovered": True})
        store.finish_job(JOB, "probe-second")
        store.checkpoint("phase1_probe", "awaiting_second_restart")
        return "PHASE1_RECOVERED_AWAITING_SECOND_RESTART"
    if stage in ("awaiting_second_restart", "verified"):
        job = store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()
        steps = store.db.execute("SELECT step_id,status,attempts FROM ai_ops_steps WHERE job_id=? ORDER BY step_id", (JOB,)).fetchall()
        assert job and job[0] == "COMPLETE"
        assert [(s[0], s[1], s[2]) for s in steps] == [("completed", "COMPLETE", 1), ("interrupted", "COMPLETE", 2)]
        assert store.claim("probe-third") is None
        assert store.health()["integrity"] == "ok"
        store.checkpoint("phase1_probe", "verified")
        return "PHASE1_SECOND_RESTART_VERIFIED" if stage == "awaiting_second_restart" else "PHASE1_VERIFIED_STABLE"
    raise RuntimeError("unknown Phase 1 probe checkpoint")
