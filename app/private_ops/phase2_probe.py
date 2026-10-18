"""One-time isolated real-volume DR failure and recovery acceptance probe."""
from __future__ import annotations

from .dr_sync import status, sync_once
from .store import Store

JOB = "phase1:railway-volume-restart-probe"


class UnavailableRepository:
    def put(self, path: str, content: bytes) -> None:
        raise ConnectionError("synthetic unavailable repository")

    def get(self, path: str) -> bytes:
        raise ConnectionError("synthetic unavailable repository")


def run(store: Store, repo: object, *, source_commit: str) -> str:
    stage = store.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='phase2_fault_probe'").fetchone()
    if stage and stage[0] == "verified":
        return "PHASE2_FAULT_RETRY_VERIFIED_STABLE"
    job = store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()
    assert job and job[0] == "COMPLETE", "existing Phase 1 job must be intact"
    if not stage:
        with store.transaction() as db:
            store._mark_dr_dirty(db)
        try:
            sync_once(store, UnavailableRepository(), worker="phase2-probe-fail", source_commit=source_commit, force=True)
        except ConnectionError:
            pass
        else:
            raise AssertionError("GitHub outage was not simulated")
        assert status(store, "github")["outcome"] == "failed"
        assert store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()[0] == "COMPLETE"
        store.checkpoint("phase2_fault_probe", "injected")
    try:
        sync_once(store, repo, worker="phase2-probe-retry", source_commit=source_commit, force=True)
    except Exception:
        # Backup failure must not prevent service readiness or local work.
        return "PHASE2_FAULT_RETRY_PENDING"
    assert status(store, "github")["outcome"] == "backed_up"
    assert store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()[0] == "COMPLETE"
    store.checkpoint("phase2_fault_probe", "verified")
    return "PHASE2_FAULT_RETRY_VERIFIED"
