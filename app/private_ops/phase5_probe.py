"""One-shot fake-provider Phase 5 shadow probe over real persisted Phase-4 packets."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from .phase5 import CLASSIFICATIONS, SUSPICIOUS, build_handoff, process_backlog
from .provider_adapters import AnalysisResult, Slot
from .provider_router import Router
from .store import Store


class _FakeAdapter:
    """Deterministic no-network provider used only by the deploy shadow probe."""
    @staticmethod
    def _packets(prompt: str) -> list[dict]:
        return json.loads(prompt.split("\nEVIDENCE=", 1)[1])

    def execute(self, slot: Slot, task, model: str, *, now: float) -> AnalysisResult:
        findings = []
        for packet in self._packets(task.prompt):
            reasons = set(packet.get("reasons", []))
            if packet.get("coverage") == "missing":
                classification, severity, subsystem = "COVERAGE_INCONCLUSIVE", "LOW", "coverage"
            elif "cancellation" in reasons or "qualify_cancel_oscillation" in reasons:
                classification, severity, subsystem = "ALERT_LIFECYCLE_ERROR", "HIGH", "alert_lifecycle"
            elif "provider_changed" in reasons:
                classification, severity, subsystem = "PROVIDER_ISSUE", "MEDIUM", "provider"
            else:
                classification, severity, subsystem = "INVESTIGATE", "MEDIUM", "prediction"
            assert classification in CLASSIFICATIONS
            samples = packet.get("samples", [])
            cpa = [s["cpa_km"] for s in samples if s.get("cpa_km") is not None][:2]
            obs = [s["observed_km"] for s in samples if s.get("observed_km") is not None][:2]
            states = [s["state"] for s in samples if s.get("state")][:4]
            findings.append({"case_ref": packet["case_ref"], "classification": classification, "severity": severity,
                             "subsystem": subsystem, "event_ids": packet.get("evidence_refs", [])[:2],
                             "cpa_km": cpa, "observed_km": obs, "states": states, "coverage": packet["coverage"],
                             "rationale": "Deterministic fake-provider shadow verification.",
                             "needs_review": classification in SUSPICIOUS or severity in ("HIGH", "CRITICAL")})
        return AnalysisResult(slot.provider, slot.name, model, {"summary": "shadow probe", "findings": findings}, 0, 0)


def run(source: Store) -> str:
    rows = source.db.execute("""SELECT packet_id,window_start,case_ref,kind,packet_json,content_hash,created
        FROM ai_ops_evidence ORDER BY created DESC,packet_id LIMIT 8""").fetchall()
    if not rows:
        raise RuntimeError("Phase 5 shadow probe requires persisted Phase-4 evidence")
    before = [(r[0], r[5]) for r in rows]
    with tempfile.TemporaryDirectory(prefix="ai-ops-phase5-") as td:
        temp = Store(Path(td))
        try:
            with temp.transaction() as db:
                for row in rows:
                    db.execute("""INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created)
                        VALUES(?,?,?,?,?,?,?)""", tuple(row))
            slots = [Slot("groq", "GROQ_KEY", "fake"), Slot("mistral", "MISTRAL_API", "fake"),
                     Slot("gemini", "GEMINI_API_KEY", "fake")]
            models = {"groq": "phase5-fake", "mistral": "phase5-fake", "gemini": "phase5-fake"}
            r = Router(temp, slots, adapter=_FakeAdapter(), approved_free_routes={(p, m) for p, m in models.items()})
            result = process_backlog(temp, r, models, worker="phase5-shadow", max_batches=32)
            pending = temp.db.execute("SELECT count(*) FROM ai_ops_cases WHERE state='PENDING_AI'").fetchone()[0]
            reviews = temp.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0]
            if pending or reviews < len(rows):
                raise RuntimeError("Phase 5 shadow pipeline did not durably process replay evidence")
            handoff = build_handoff(temp)
            if "credential" in json.dumps(handoff).lower():
                raise RuntimeError("unsafe handoff content")
        finally:
            temp.close()
    after = [(r[0], r[1]) for r in source.db.execute("SELECT packet_id,content_hash FROM ai_ops_evidence WHERE packet_id IN (%s) ORDER BY created DESC,packet_id" % ",".join("?" * len(rows)), [r[0] for r in rows])]
    # Verify source evidence is immutable. Set comparison avoids query ordering differences.
    if set(before) != set(after):
        raise RuntimeError("shadow probe altered source evidence")
    return f"PHASE5_SHADOW_REPLAY_VERIFIED packets={len(rows)} reviews={reviews} batches={result['complete']}"
