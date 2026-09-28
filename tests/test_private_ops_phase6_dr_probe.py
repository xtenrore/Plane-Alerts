import json

import pytest

from app.private_ops.phase6_dr_probe import run
from app.private_ops.store import Store

COMMIT = "a" * 40


class MemoryRepository:
    def __init__(self):
        self.files = {}

    def put(self, path, content):
        prior = self.files.get(path)
        if prior is not None and prior != content:
            raise RuntimeError("immutable snapshot conflict")
        self.files[path] = content

    def get(self, path):
        return self.files[path]


def test_live_volume_roundtrip_preserves_phase1_and_phase6_state(tmp_path):
    store = Store(tmp_path)
    store.checkpoint("phase1_probe", "verified_extended")
    repo = MemoryRepository()
    assert run(store, repo, source_commit=COMMIT) == "PHASE6_DR_LIVE_ROUNDTRIP_VERIFIED"
    assert store.db.execute("SELECT synced_revision=revision FROM ai_ops_dr_sync WHERE destination='github'").fetchone()[0] == 1
    manifest = json.loads(next(v for k, v in repo.files.items() if k.endswith(".manifest.json")))
    assert manifest["schema"] == 4 and manifest["source_commit"] == COMMIT
    assert store.health()["schema"] == 6
    store.close()


def test_missing_marker_never_writes_remote(tmp_path):
    store = Store(tmp_path)
    repo = MemoryRepository()
    with pytest.raises(RuntimeError, match="stable marker"):
        run(store, repo, source_commit=COMMIT)
    assert not repo.files
    store.close()


def test_remote_readback_failure_keeps_retryable_revision(tmp_path):
    store = Store(tmp_path)
    store.checkpoint("phase1_probe", "verified_extended")
    class Broken(MemoryRepository):
        def get(self, path):
            raise OSError("transient remote outage")
    with pytest.raises(OSError):
        run(store, Broken(), source_commit=COMMIT)
    row = store.db.execute("SELECT revision,synced_revision,retry_count FROM ai_ops_dr_sync WHERE destination='github'").fetchone()
    assert row[0] > row[1] and row[2] > 0
    store.close()
