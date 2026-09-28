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
from app.private_ops.provider_adapters import Adapter, AnalysisTask, ProviderFailure, Slot
from app.private_ops.provider_router import NoFreeRoute, Router
from app.private_ops.store import Store


class CanaryAdapter(Adapter):
    def execute(self, slot: Slot, task: AnalysisTask, model: str, *, now: float):
        try:
            return super().execute(slot, task, model, now=now)
        except ProviderFailure as exc:
            # Only status/class/scope; provider response and credentials stay private.
            print("PHASE6_CANARY_PROVIDER_FAILURE provider=" + slot.provider +
                  " kind=" + exc.kind + " http_status=" + str(exc.status) +
                  " scope=" + ("provider" if exc.provider_wide else "unspecified"), flush=True)
            raise


def run_pair(store: Store, packet_id: str, router: Router, models: dict[str, str],
             second_provider: str = "mistral") -> tuple[str, str | None]:
    """Exactly one call per independent provider; no rotation or deep reviewer."""
    if second_provider not in ("mistral", "gemini") or second_provider not in models:
        raise ValueError("approved independent provider required")
    packet = _packet(store, packet_id)
    case = str(packet["case_ref"])
    results = []
    for role, provider in (("triage", "groq"), ("independent_review", second_provider)):
        prompt = _build_prompt(role, [packet])
        try:
            result = router.execute(AnalysisTask(role, prompt, batch_id="phase6:shadow", packet_id=packet_id),
                                    models, preferred_providers=[provider],
                                    exclude_providers=[p for p in models if p != provider], max_attempts=1)
        except NoFreeRoute:
            usage = store.db.execute("SELECT provider,failure_kind FROM ai_ops_usage ORDER BY id DESC LIMIT 1").fetchone()
            kind = str(usage[1]) if usage and usage[0] == provider else "not_attempted"
            if results:
                # Retain the validated first opinion, and leave only the
                # independent stage pending. Never treat partial review as
                # agreement or a completed canary.
                with store.transaction() as db:
                    db.execute("""UPDATE ai_ops_cases SET state='PENDING_AI',pending_role='independent_review',
                        classification=?,validation_status='VALID',last_error=?,updated=strftime('%s','now')
                        WHERE packet_id=?""", (results[0], "independent_review_" + kind, packet_id))
                    store._mark_dr_dirty(db)
                return results[0], None
            raise RuntimeError(role + "_free_route_unavailable_failure_kind=" + kind) from None
        validated, shape = _strict_batch({case: packet}, result.analysis)
        if not shape or len(validated) != 1 or validated[0].validation_status != "VALID":
            errors = ",".join(validated[0].validation_errors) if validated else "invalid_shape"
            raise RuntimeError(role + "_evidence_validation_failed:" + errors)
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
           "mistral": "https://api.mistral.ai/v1/models",
           "gemini": "https://generativelanguage.googleapis.com/v1beta/models"}[provider]
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        headers = ({"x-goog-api-key": credential} if provider == "gemini"
                   else {"Authorization": "Bearer " + credential})
        response = client.get(url, headers=headers)
    if response.status_code != 200 or len(response.content) > 131072:
        raise RuntimeError(provider + "_catalog_unavailable")
    data = response.json()
    if provider == "gemini":
        identifiers = sorted(str(item["name"]).removeprefix("models/") for item in data.get("models", [])
                             if isinstance(item, dict) and isinstance(item.get("name"), str)
                             and "generateContent" in item.get("supportedGenerationMethods", []))
    else:
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
    parser.add_argument("--evidence-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("AI_OPS_PAID_USAGE_ALLOWED", "false").lower() != "false":
        raise RuntimeError("paid usage policy must remain false")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        raise ValueError("source commit required")
    if not re.fullmatch(r"[0-9a-f]{40}", args.evidence_commit):
        raise ValueError("evidence commit required")
    hour = datetime.fromisoformat(args.hour.replace("Z", "+00:00"))
    if hour.tzinfo is None or hour.utcoffset().total_seconds() or hour.minute or hour.second or hour.microsecond:
        raise ValueError("closed UTC hour required")
    second_provider = os.environ.get("AI_OPS_CANARY_SECOND_PROVIDER", "mistral")
    if second_provider not in ("mistral", "gemini"):
        raise RuntimeError("approved independent provider required")
    names = {"groq": "GROQ_KEY", "mistral": "MISTRAL_API", "gemini": "GEMINI_API_KEY"}
    models = {"groq": os.environ.get("AI_OPS_CANARY_GROQ_MODEL", ""),
              second_provider: os.environ.get("AI_OPS_CANARY_" + second_provider.upper() + "_MODEL", "")}
    slots = []
    unavailable = []
    for provider in models:
        name = names[provider]
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
            router = Router(store, slots, adapter=CanaryAdapter(), approved_free_routes=set(models.items()))
            first, second = run_pair(store, packet_id, router, models, second_provider)
            data, manifest = export(store, source_commit=args.source_commit)
            restored_root = Path(td) / "reopen"
            restored_root.mkdir()
            reopened = restore_empty(restored_root, data, manifest)
            try:
                reviews = reopened.db.execute("SELECT role,provider,model,independence,validation_status FROM ai_ops_reviews ORDER BY created").fetchall()
                expected = 1 if second is None else 2
                if (len(reviews) != expected or reviews[0][1] != "groq" or
                        (second is not None and (reviews[1][1] != second_provider or
                                                 reviews[1][3] != "DIFFERENT_PROVIDER_AND_MODEL_BLIND"))):
                    raise RuntimeError("independent_reviews_not_durable")
            finally:
                reopened.close()
            payload = {"schema": 1, "source_commit": args.source_commit, "evidence_commit": args.evidence_commit,
                       "manifest": manifest,
                       "snapshot": json.loads(data)}
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            if len(raw) > 100000:
                raise RuntimeError("canary_checkpoint_exceeds_bound")
            args.output.write_bytes(raw)
            args.output.with_suffix(".status").write_text("COMPLETE" if second is not None else "PENDING_AI")
            if second is None:
                failure = store.db.execute("SELECT failure_kind FROM ai_ops_usage WHERE provider=? ORDER BY id DESC LIMIT 1",
                                           (second_provider,)).fetchone()
                failure_kind = str(failure[0]) if failure and failure[0] in (
                    "quota", "auth", "server", "network", "timeout", "malformed", "schema", "context", "request") else "unavailable"
                print("PHASE6_REAL_CANARY_PARTIAL_PENDING_AI provider=groq reviews=1 "
                      "second=" + second_provider + "-" + failure_kind + " checkpoint_sha256=" + hashlib.sha256(raw).hexdigest(), flush=True)
            else:
                print("PHASE6_REAL_CANARY_VALIDATED providers=groq," + second_provider + " reviews=2 "
                      "independence=blind-different-family agreement=" + ("yes" if first == second else "no") +
                      " checkpoint_sha256=" + hashlib.sha256(raw).hexdigest(), flush=True)
        finally:
            store.close()


if __name__ == "__main__":
    main()
