import json

import pytest

from app.private_ops.dr_export import export, restore_empty
from app.private_ops.store import Store
from app.private_ops import phase5
from app.private_ops.provider_adapters import AnalysisResult, Slot
from app.private_ops.provider_router import Router

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


def _packet():
    return {"schema": 1, "window_start": 100, "case_ref": "CASE-DR", "evidence_refs": ["EV-DR"],
            "event_count": 1, "reasons": ["cancellation"], "coverage": "good",
            "samples": [{"kind": "outcome", "state": "CANCELLED", "cpa_km": 4.2, "observed_km": 4.4}]}


def test_phase5_canonical_lifecycle_survives_sanitized_disaster_recovery(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir(); target.mkdir()
    store = Store(source)
    packet = _packet()
    content = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    pid = Store.digest(packet)
    store.db.execute("INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created) VALUES(?,?,?,?,?,?,0)",
                     (pid, 100, "CASE-DR", "cancellation", content, pid))
    phase5.seed_pending_cases(store)
    result = phase5.Validated("CASE-DR", "ALERT_LIFECYCLE_ERROR", "HIGH", "alert_lifecycle", ("EV-DR",), (4.2,), (4.4,),
                              ("CANCELLED",), "good", "Evidence-backed lifecycle anomaly.", True)
    finding, _ = phase5.upsert_finding(store, pid, result)
    phase5._persist_review(store, pid, "triage", "groq", "g-free", "GROQ_KEY", result)
    store.db.execute("UPDATE ai_ops_cases SET state='REVIEWING',pending_role='independent_review',finding_id=? WHERE packet_id=?", (finding, pid))
    phase5.transition_finding(store, finding, "REVIEWING")
    data, manifest = export(store, source_commit=COMMIT)
    store.close()
    restored = restore_empty(target, data, manifest)
    assert manifest["cases"] == 1 and manifest["reviews"] == 1 and manifest["findings"] == 1
    assert restored.db.execute("SELECT state,pending_role,finding_id FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[:] == (
        "REVIEWING", "independent_review", finding)
    review = json.loads(restored.db.execute("SELECT result_json FROM ai_ops_reviews WHERE packet_id=?", (pid,)).fetchone()[0])
    assert review["classification"] == "ALERT_LIFECYCLE_ERROR" and review["rationale"] == "Evidence-backed lifecycle anomaly."
    assert restored.db.execute("SELECT status FROM ai_ops_findings WHERE finding_id=?", (finding,)).fetchone()[0] == "REVIEWING"
    restored.close()


def test_unfinished_phase5_model_call_is_retriable_after_sanitized_dr(tmp_path):
    source = tmp_path / "source"; target = tmp_path / "target"
    source.mkdir(); target.mkdir()
    store = Store(source)
    packet = _packet(); content = json.dumps(packet, sort_keys=True, separators=(",", ":")); pid = Store.digest(packet)
    store.db.execute("INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created) VALUES(?,?,?,?,?,?,0)",
                     (pid, 100, "CASE-DR", "cancellation", content, pid))
    phase5.seed_pending_cases(store); [bid] = phase5.queue_triage_batches(store)
    assert store.claim("worker") == bid
    assert store.begin_step(bid, "model_call", "worker", {"role": "triage", "packet_ids": [pid], "repair": False})
    store.complete_step(bid, "model_call", "worker", {"analysis": {"summary": "ok", "findings": []}, "provider": "groq", "model": "g-free", "slot": "GROQ_KEY"})
    data, manifest = export(store, source_commit=COMMIT)
    store.close()
    restored = restore_empty(target, data, manifest)
    assert restored.db.execute("SELECT status FROM ai_ops_jobs WHERE id=?", (bid,)).fetchone()[0] == "RETRY"
    assert restored.db.execute("SELECT status,output_json FROM ai_ops_steps WHERE job_id=? AND step_id='model_call'", (bid,)).fetchone()[:] == ("RETRY", None)
    assert restored.claim("recovery") == bid
    assert restored.begin_step(bid, "model_call", "recovery", {"role": "triage", "packet_ids": [pid], "repair": False})
    restored.close()
