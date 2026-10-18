from app.private_ops.dr_sync import status
from app.private_ops.phase2_probe import JOB, run
from app.private_ops.store import Store


class MemoryRepo:
    def __init__(self):
        self.files = {}

    def put(self, path, content):
        self.files.setdefault(path, content)

    def get(self, path):
        return self.files[path]


def test_live_state_unaffected_by_outage_and_retry_idempotent(tmp_path):
    store = Store(tmp_path)
    store.enqueue(JOB)
    store.claim("probe")
    store.finish_job(JOB, "probe")
    repo = MemoryRepo()
    assert run(store, repo, source_commit="a" * 40) == "PHASE2_FAULT_RETRY_VERIFIED"
    assert status(store, "github")["last_hash"]
    assert store.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (JOB,)).fetchone()[0] == "COMPLETE"
    initial = dict(repo.files)
    assert run(store, repo, source_commit="a" * 40) == "PHASE2_FAULT_RETRY_VERIFIED_STABLE"
    assert repo.files == initial
    store.close()
