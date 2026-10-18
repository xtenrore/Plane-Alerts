import hashlib
import json

from app.private_ops.phase5_probe import run
from app.private_ops.store import Store


def _packet(case_ref="CASE-PROBE"):
    return {
        "schema": 1,
        "window_start": 1790500000,
        "case_ref": case_ref,
        "evidence_refs": [case_ref + "-evt-1", case_ref + "-evt-2"],
        "event_count": 2,
        "reasons": ["cancellation"],
        "coverage": "good",
        "samples": [
            {"kind": "prediction", "state": "QUALIFIED", "cpa_km": 4.2, "observed_km": None},
            {"kind": "outcome", "state": "CANCELLED", "cpa_km": 18.7, "observed_km": 4.6},
        ],
    }


def test_empty_persistent_store_is_safe_noop(tmp_path):
    store = Store(tmp_path)
    try:
        assert run(store) == "PHASE5_SHADOW_SKIPPED_NO_PERSISTED_EVIDENCE"
        assert store.health()["integrity"] == "ok"
    finally:
        store.close()


def test_persisted_sanitized_packet_is_replayed_without_mutation(tmp_path):
    store = Store(tmp_path)
    packet = _packet()
    content = json.dumps(packet, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(content.encode()).hexdigest()
    with store.transaction() as db:
        db.execute(
            """INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created)
               VALUES(?,?,?,?,?,?,?)""",
            (digest, packet["window_start"], packet["case_ref"], "cancellation", content, digest, 1.0),
        )
    before = store.db.execute("SELECT packet_json,content_hash FROM ai_ops_evidence WHERE packet_id=?", (digest,)).fetchone()
    try:
        result = run(store)
        assert result.startswith("PHASE5_SHADOW_REPLAY_VERIFIED packets=1 reviews=")
        after = store.db.execute("SELECT packet_json,content_hash FROM ai_ops_evidence WHERE packet_id=?", (digest,)).fetchone()
        assert tuple(after) == tuple(before)
    finally:
        store.close()
