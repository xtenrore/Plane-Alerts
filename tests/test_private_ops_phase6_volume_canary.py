"""A real-volume shaped shadow checkpoint must reopen without model calls."""
import json

import pytest

from app.private_ops.dr_export import export
from app.private_ops.phase6_volume_canary import run
from app.private_ops.provider_adapters import Slot
from app.private_ops.provider_router import Router
from app.private_ops.store import Store
from scripts.private_ai_ops_phase6_canary import run_pair
from test_private_ops_phase5 import FREE, MODELS, ScenarioAdapter, queued_store


SHA = "a" * 40


class Repository:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def get(self, path):
        assert path == "private-ai-ops/snapshots/phase6-canary-" + SHA + ".json"
        self.calls += 1
        return self.payload


def payload_from_two_reviews(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    store, (packet_id,) = queued_store(origin)
    adapter = ScenarioAdapter()
    route = Router(store, [Slot("groq", "GROQ_KEY", "fake"), Slot("gemini", "GEMINI_API_KEY", "fake")],
                   adapter=adapter, approved_free_routes=FREE)
    assert all(run_pair(store, packet_id, route, MODELS, "gemini"))
    data, manifest = export(store, source_commit=SHA)
    store.close()
    return json.dumps({"schema": 1, "source_commit": SHA, "evidence_commit": "b" * 40,
                       "snapshot": json.loads(data), "manifest": manifest}, sort_keys=True).encode()


def test_real_volume_shadow_restore_and_restart_reuse(tmp_path):
    repo = Repository(payload_from_two_reviews(tmp_path))
    live = tmp_path / "live"
    live.mkdir()
    original = Store(live)
    original.enqueue("existing-work")
    original.close()
    assert "reviews=2" in run(live, repo, source_commit=SHA)
    assert "reviews=2" in run(live, repo, source_commit=SHA)
    assert repo.calls == 1
    shadow = Store(live / "phase6-shadow-canary" / SHA)
    assert shadow.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 2
    shadow.close()
    original = Store(live)
    assert original.db.execute("SELECT count(*) FROM ai_ops_jobs").fetchone()[0] == 1
    original.close()


def test_reject_incomplete_or_tampered_checkpoint_without_success_marker(tmp_path):
    payload = json.loads(payload_from_two_reviews(tmp_path))
    payload["snapshot"]["reviews"] = payload["snapshot"]["reviews"][:1]
    repo = Repository(json.dumps(payload).encode())
    with pytest.raises(ValueError, match="checksum"):
        run(tmp_path / "volume", repo, source_commit=SHA)
    assert not (tmp_path / "volume" / "phase6-shadow-canary" / SHA).exists()


def test_reject_source_commit_escape(tmp_path):
    with pytest.raises(ValueError, match="exact source"):
        run(tmp_path, Repository(b"{}"), source_commit="../outside")
