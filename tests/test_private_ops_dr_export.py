import json

import pytest

from app.private_ops.dr_export import export, restore_empty
from app.private_ops.store import Store

COMMIT = "a" * 40


def test_sanitized_checkpoint_restores_into_clean_directory(tmp_path):
    origin = tmp_path / "origin"
    target = tmp_path / "target"
    origin.mkdir()
    target.mkdir()
    store = Store(origin)
    store.enqueue("audit:test")
    store.claim("worker")
    store.begin_step("audit:test", "step-one", "worker", {"case": 1})
    store.complete_step("audit:test", "step-one", "worker", {"ok": True})
    store.begin_step("audit:test", "interrupted", "worker", {"case": 2})
    store.checkpoint("audit-hour", "done")
    data, manifest = export(store, source_commit=COMMIT)
    store.close()
    assert manifest["jobs"] == 1 and manifest["steps"] == 2
    restored = restore_empty(target, data, manifest)
    assert restored.health()["integrity"] == "ok"
    assert restored.db.execute("SELECT output_json FROM ai_ops_steps WHERE step_id='step-one'").fetchone()[0] is None
    assert restored.db.execute("SELECT output_hash FROM ai_ops_steps WHERE step_id='step-one'").fetchone()[0] == Store.digest({"ok": True})
    assert restored.db.execute("SELECT status FROM ai_ops_steps WHERE step_id='interrupted'").fetchone()[0] == "RETRY"
    assert restored.claim("recovery-worker") == "audit:test"
    assert not restored.begin_step("audit:test", "step-one", "recovery-worker", {"case": 1})
    assert restored.begin_step("audit:test", "interrupted", "recovery-worker", {"case": 2})
    assert restored.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='audit-hour'").fetchone()[0] == "done"
    restored.close()
    with pytest.raises(FileExistsError):
        restore_empty(target, data, manifest)


def test_export_omits_output_payload_even_when_it_contains_secrets_and_coordinates(tmp_path):
    store = Store(tmp_path)
    store.enqueue("audit:test")
    store.claim("worker")
    store.begin_step("audit:test", "step-one", "worker", {})
    store.complete_step("audit:test", "step-one", "worker", {"api_key": "secret"})
    data, _ = export(store, source_commit=COMMIT)
    assert b"api_key" not in data and b"secret" not in data
    store.db.execute("UPDATE ai_ops_steps SET output_json=?", (json.dumps({"latitude": 41.0}),))
    data, _ = export(store, source_commit=COMMIT)
    assert b"latitude" not in data and b"41.0" not in data
    store.close()


def test_corrupt_export_never_overwrites_healthy_state(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    store = Store(source)
    data, manifest = export(store, source_commit=COMMIT)
    store.close()
    with pytest.raises(ValueError, match="checksum"):
        restore_empty(target, data + b"!", manifest)
    assert not list(target.iterdir())
    healthy = Store(target)
    with pytest.raises(FileExistsError):
        restore_empty(target, data, manifest)
    assert healthy.health()["integrity"] == "ok"
    healthy.close()
