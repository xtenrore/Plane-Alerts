"""One-shot, free-only shadow inference on one real sanitized Phase 4 packet.

Credentials are read only from this GitHub Actions process. The isolated
Railway service receives a sanitized checkpoint, never provider credentials.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.private_ops.collector import collect
from app.private_ops.dr_export import export, restore_empty
from app.private_ops.phase5 import (_build_prompt, _packet, _persist_review, _strict_batch,
                                    seed_pending_cases)
from app.private_ops.prediction_source import read_hour
from app.private_ops.provider_adapters import Adapter, AnalysisTask, Slot
from app.private_ops.provider_router import Router
from app.private_ops.store import Store


def run_pair(store: Store, packet_id: str, router: Router, models: dict[str, str]) -> tuple[str, str]:
    """Exactly one call per provider; no rotation and no deep reviewer."""
    packet = _packet(store, packet_id)
    case = str(packet["case_ref"])
    results = []
    for role, provider in (("triage", "groq"), ("independent_review", "mistral")):
        prompt = _build_prompt(role, [packet])
        result = router.execute(AnalysisTask(role, prompt, batch_id="phase6:shadow", packet_id=packet_id),
                                models, preferred_providers=[provider],
                                exclude_providers=[p for p in models if p != provider], max_attempts=1)
        validated, shape = _strict_batch({case: packet}, result.analysis)
        if not shape or len(validated) != 1 or validated[0].validation_status != "VALID":
            raise RuntimeError(role + "_evidence_validation_failed")
        agreement = None if not results else "AGREE" if results[0] == validated[0].classification else "DISAGREE"
        _persist_review(store, packet_id, role, result.provider, result.model, result.slot_name,
                        validated[0], agreement=agreement)
        results.append(validated[0].classification)
    with store.transaction() as db:
        db.execute("""UPDATE ai_ops_cases SET state='INVESTIGATING',pending_role='',classification=?,
            validation_status='VALID',escalation_reason=?,updated=strftime('%s','now') WHERE packet_id=?""",
            (results[0], "DISAGREEMENT" if results[0] != results[1] else "SHADOW_AGREEMENT", packet_id))
        store._mark_dr_dirty(db)
    return tuple(results)


def _catalog_contains(provider: str, model: str, credential: str) -> bool:
    import httpx
    url = {"groq": "https://api.groq.com/openai/v1/models",
           "mistral": "https://api.mistral.ai/v1/models"}[provider]
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        response = client.get(url, headers={"Authorization": "Bearer " + credential})
    if response.status_code != 200 or len(response.content) > 131072:
        raise RuntimeError(provider + "_catalog_unavailable")
    data = response.json()
    identifiers = sorted(str(item["id"]) for item in data.get("data", [])
                         if isinstance(item, dict) and isinstance(item.get("id"), str))
    if model not in identifiers:
        # Model names are public metadata; never print the request or credential.
        print(provider + "_CATALOG_MODEL_IDS=" + ",".join(identifiers[:40]), flush=True)
    return model in identifiers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--hour", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("AI_OPS_PAID_USAGE_ALLOWED", "false").lower() != "false":
        raise RuntimeError("paid usage policy must remain false")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        raise ValueError("source commit required")
    hour = datetime.fromisoformat(args.hour.replace("Z", "+00:00"))
    if hour.tzinfo is None or hour.utcoffset().total_seconds() or hour.minute or hour.second or hour.microsecond:
        raise ValueError("closed UTC hour required")
    names = {"groq": "GROQ_KEY", "mistral": "MISTRAL_API"}
    models = {"groq": os.environ.get("AI_OPS_CANARY_GROQ_MODEL", ""),
              "mistral": os.environ.get("AI_OPS_CANARY_MISTRAL_MODEL", "")}
    slots = []
    unavailable = []
    for provider, name in names.items():
        key = os.environ.get(name, "")
        if not key or not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", models[provider]):
            raise RuntimeError(provider + "_free_route_not_configured")
        if not _catalog_contains(provider, models[provider], key):
            unavailable.append(provider)
        slots.append(Slot(provider, name, key))
    if unavailable:
        raise RuntimeError("approved_model_not_in_current_catalog:" + ",".join(unavailable))
    start = int(hour.timestamp())
    with tempfile.TemporaryDirectory(prefix="phase6-canary-") as td:
        store = Store(Path(td))
        try:
            collected = collect(store, start=start, end=start + 3600,
                                records=read_hour(args.source, start, start + 3600), source_complete=True)
            if collected.packets < 1:
                raise RuntimeError("no_real_sanitized_packet")
            rows = store.db.execute("SELECT packet_id,packet_json FROM ai_ops_evidence ORDER BY packet_id LIMIT 8").fetchall()
            packet_id = next((row[0] for row in rows if json.loads(row[1]).get("coverage") == "good"), rows[0][0])
            # Keep the shadow checkpoint to one real packet. The canonical
            # Prediction Lab history remains in its original source branch.
            with store.transaction() as db:
                db.execute("DELETE FROM ai_ops_evidence WHERE packet_id<>?", (packet_id,))
            seed_pending_cases(store)
            router = Router(store, slots, adapter=Adapter(), approved_free_routes=set(models.items()))
            first, second = run_pair(store, packet_id, router, models)
            data, manifest = export(store, source_commit=args.source_commit)
            restored_root = Path(td) / "reopen"
            restored_root.mkdir()
            reopened = restore_empty(restored_root, data, manifest)
            try:
                reviews = reopened.db.execute("SELECT role,provider,model,independence,validation_status FROM ai_ops_reviews ORDER BY created").fetchall()
                if len(reviews) != 2 or reviews[0][1] != "groq" or reviews[1][1] != "mistral" or reviews[1][3] != "DIFFERENT_PROVIDER_AND_MODEL_BLIND":
                    raise RuntimeError("independent_reviews_not_durable")
            finally:
                reopened.close()
            payload = {"schema": 1, "source_commit": args.source_commit, "manifest": manifest,
                       "snapshot": json.loads(data)}
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > 100000:
                raise RuntimeError("canary_checkpoint_exceeds_bound")
            args.output.write_bytes(raw)
            print("PHASE6_REAL_CANARY_VALIDATED providers=groq,mistral reviews=2 "
                  "independence=blind-different-family agreement=" + ("yes" if first == second else "no") +
                  " checkpoint_sha256=" + hashlib.sha256(raw).hexdigest(), flush=True)
        finally:
            store.close()


if __name__ == "__main__":
    main()
