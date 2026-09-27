import pytest

from app.private_ops.dr_export import restore_empty
from app.private_ops.dr_sync import status, sync_once
from app.private_ops.store import Store

COMMIT = "b" * 40


class MemoryRepo:
    def __init__(self):
        self.files = {}
        self.fail = False
        self.corrupt = False

    def put(self, path, content):
        if self.fail:
            raise ConnectionError("remote unavailable")
        self.files[path] = content

    def get(self, path):
        if self.fail:
            raise ConnectionError("remote unavailable")
        return self.files[path] + (b"tampered" if self.corrupt else b"")


def test_coalesced_sync_remote_readback_clean_restore_and_independent_status(tmp_path):
    origin, target = tmp_path / "source", tmp_path / "recovered"
    origin.mkdir()
    target.mkdir()
    store = Store(origin)
    store.enqueue("audit:test")
    store.claim("worker")
    store.begin_step("audit:test", "done", "worker", {"item": 1})
    store.complete_step("audit:test", "done", "worker", {"password": "must never upload"})
    store.begin_step("audit:test", "interrupted", "worker", {"item": 2})
    repo = MemoryRepo()
    assert not sync_once(store, repo, worker="a", source_commit=COMMIT, now=1)
    assert sync_once(store, repo, worker="a", source_commit=COMMIT, now=10000000000, force=True)
    assert len(repo.files) == 2
    assert all(b"password" not in value and b"must never upload" not in value for value in repo.files.values())
    assert status(store, "github")["outcome"] == "backed_up"
    assert status(store, "dropbox")["outcome"] == "pending"
    files = list(repo.files.values())
    data = next(x for x in files if b'"jobs":[' in x)
    import json
    manifest = json.loads(next(x for x in files if b'"sha256"' in x))
    restored = restore_empty(target, data, manifest)
    assert restored.claim("new-host") == "audit:test"
    assert not restored.begin_step("audit:test", "done", "new-host", {"item": 1})
    assert restored.begin_step("audit:test", "interrupted", "new-host", {"item": 2})
    restored.close()
    store.close()


def test_network_and_remote_corruption_retry_without_losing_local_jobs(tmp_path):
    store = Store(tmp_path)
    store.enqueue("audit:test")
    repo = MemoryRepo()
    repo.fail = True
    with pytest.raises(ConnectionError):
        sync_once(store, repo, worker="one", source_commit=COMMIT, now=10000000000, force=True)
    assert status(store, "github")["outcome"] == "failed"
    assert store.claim("worker") == "audit:test"
    repo.fail = False
    repo.corrupt = True
    with pytest.raises(IOError, match="read-back"):
        sync_once(store, repo, worker="two", source_commit=COMMIT, now=10000001000, force=True)
    repo.corrupt = False
    assert sync_once(store, repo, worker="three", source_commit=COMMIT, now=10000002000, force=True)
    assert status(store, "github")["outcome"] == "backed_up"
    store.close()


def test_lease_blocks_parallel_claim_and_new_revision_survives_sync(tmp_path):
    store = Store(tmp_path)
    store.enqueue("audit:test")
    store.db.execute("UPDATE ai_ops_dr_sync SET lease_owner='other',lease_until=99999999999 WHERE destination='github'")
    repo = MemoryRepo()
    assert not sync_once(store, repo, worker="contender", source_commit=COMMIT, now=10000000000, force=True)
    assert repo.files == {}
    store.db.execute("UPDATE ai_ops_dr_sync SET lease_until=0 WHERE destination='github'")
    assert sync_once(store, repo, worker="contender", source_commit=COMMIT, now=10000000000, force=True)
    store.enqueue("audit:later")
    assert status(store, "github")["outcome"] == "pending"
    assert status(store, "github")["revision"] == 2
    store.close()


def test_existing_phase1_schema_migrates_without_losing_job_state(tmp_path):
    import sqlite3
    path = tmp_path / "ai_ops.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE ai_ops_jobs(id TEXT PRIMARY KEY,status TEXT,priority INTEGER,created REAL,updated REAL,lease_owner TEXT,lease_until REAL);
        CREATE TABLE ai_ops_steps(job_id TEXT,step_id TEXT,status TEXT,input_hash TEXT,output_hash TEXT,output_json TEXT,attempts INTEGER,lease_owner TEXT,lease_until REAL,PRIMARY KEY(job_id,step_id));
        CREATE TABLE ai_ops_attempts(id INTEGER PRIMARY KEY,job_id TEXT,step_id TEXT,worker TEXT,started REAL,ended REAL,result TEXT);
        CREATE TABLE ai_ops_scheduler(name TEXT PRIMARY KEY,checkpoint TEXT,updated REAL);
        INSERT INTO ai_ops_jobs(id,status,priority,created,updated) VALUES('phase1:existing','COMPLETE',0,0,0);
        PRAGMA user_version=1;""")
    db.close()
    store = Store(tmp_path)
    assert store.health()["schema"] == 4
    assert store.db.execute("SELECT status FROM ai_ops_jobs WHERE id='phase1:existing'").fetchone()[0] == "COMPLETE"
    assert status(store, "github")["outcome"] == "pending"
    assert status(store, "github")["revision"] == 1
    store.close()
