"""Evidence gates for the independent review checkpoint."""
import json

import pytest

from app.private_ops import phase5
from app.private_ops.dr_export import export, restore_empty
from app.private_ops.provider_adapters import Slot
from app.private_ops.provider_adapters import ProviderFailure
from app.private_ops.provider_router import Router
from app.private_ops.store import Store
from test_private_ops_phase5 import (
    FREE, MODELS, ScenarioAdapter, add_packet, item_for, packet, queued_store,
)
from scripts.private_ai_ops_phase6_canary import run_pair


def route(store, adapter, slots=None):
    return Router(store, slots or [Slot("groq", "GROQ_KEY", "fake"),
                                   Slot("mistral", "MISTRAL_API", "fake"),
                                   Slot("gemini", "GEMINI_API_KEY", "fake")],
                  adapter=adapter, approved_free_routes=FREE)


def test_one_packet_shadow_canary_is_blind_durable_and_never_calls_deep(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    adapter = ScenarioAdapter()
    first, second = run_pair(store, pid, route(store, adapter), MODELS)
    assert first == second
    assert [provider for provider, _, _, _ in adapter.calls] == ["groq", "mistral"]
    rows = store.db.execute("SELECT role,provider,independence FROM ai_ops_reviews ORDER BY created").fetchall()
    assert [tuple(row) for row in rows] == [
        ("triage", "groq", "NOT_APPLICABLE"),
        ("independent_review", "mistral", "DIFFERENT_PROVIDER_AND_MODEL_BLIND"),
    ]
    saved, manifest = export(store, source_commit="a" * 40)
    store.close()
    (tmp_path / "restored").mkdir()
    restored = restore_empty(tmp_path / "restored", saved, manifest)
    assert restored.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 2
    restored.close()


def test_provider_prompt_explicitly_describes_outer_json_shape():
    prompt = phase5._build_prompt("triage", [packet()])
    assert "only summary (a short string) and findings (an array)" in prompt
    assert "cpa_km and observed_km are arrays of numbers" in prompt


def test_shadow_canary_preserves_valid_first_review_when_second_has_no_free_quota(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    adapter = ScenarioAdapter(failures={"MISTRAL_API": [ProviderFailure("quota", status=429, provider_wide=True)]})
    first, second = run_pair(store, pid, route(store, adapter), MODELS)
    assert first and second is None
    assert store.db.execute("SELECT state,pending_role FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[:] == (
        "PENDING_AI", "independent_review")
    snapshot, manifest = export(store, source_commit="a" * 40)
    store.close()
    (tmp_path / "resume").mkdir()
    reopened = restore_empty(tmp_path / "resume", snapshot, manifest)
    assert reopened.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 1
    assert reopened.db.execute("SELECT pending_role FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "independent_review"
    reopened.close()


def test_low_risk_agreement_needs_no_third_call(tmp_path):
    store = Store(tmp_path)
    p = packet(reason="trajectory_changed")
    pid = add_packet(store, p)
    phase5.seed_pending_cases(store)
    phase5.queue_triage_batches(store)
    # Explicit review requested, while the classification is a low-risk observed change.
    class LowRisk(ScenarioAdapter):
        def execute(self, slot, task, model, *, now):
            result = super().execute(slot, task, model, now=now)
            result.analysis["findings"][0] = item_for(p, "EXPECTED_TURN", "LOW")
            result.analysis["findings"][0]["needs_review"] = True
            return result
    a = LowRisk()
    router = route(store, a)
    assert phase5.process_next(store, router, MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, router, MODELS) == "COMPLETE"
    assert store.db.execute("SELECT state FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "INVESTIGATING"
    assert len(a.calls) == 2
    assert phase5.queue_review_batches(store) == []
    store.close()


def test_high_impact_agreement_escalates_and_preserves_finding(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    adapter = ScenarioAdapter()
    router = route(store, adapter)
    assert phase5.process_next(store, router, MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, router, MODELS) == "COMPLETE"
    row = store.db.execute("SELECT state,escalation_reason FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()
    assert tuple(row) == ("DEEP_REVIEW", "HIGH_IMPACT_AGREEMENT")
    review = store.db.execute("SELECT independence FROM ai_ops_reviews WHERE role='independent_review'").fetchone()[0]
    assert review == "DIFFERENT_PROVIDER_AND_MODEL_BLIND"
    phase5.queue_review_batches(store)
    strong = "x-free-strong"
    deep = Router(store, router.slots, adapter=adapter, approved_free_routes=FREE | {("gemini", strong)})
    assert phase5.process_next(store, deep, {**MODELS, "deep:gemini": strong}) == "COMPLETE"
    assert store.db.execute("SELECT escalation_reason FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "DETERMINISTIC_INVESTIGATION_REQUIRED"
    assert store.db.execute("SELECT classification FROM ai_ops_findings").fetchone()[0] == "ALERT_LIFECYCLE_ERROR"
    store.close()


def test_same_provider_key_rotation_cannot_count_as_second_opinion(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    slots = [Slot("groq", "GROQ_KEY", "fake"), Slot("groq", "GROQ_KEY_2", "fake")]
    assert phase5.process_next(store, route(store, ScenarioAdapter(), slots), MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, route(store, ScenarioAdapter(), slots), MODELS) == "PENDING_AI"
    assert store.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 1
    assert store.db.execute("SELECT pending_role FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "independent_review"
    with pytest.raises(ValueError, match="different provider"):
        phase5._persist_review(store, pid, "independent_review", "groq", "g-free", "GROQ_KEY_2",
                               phase5.validate_result(phase5._packet(store, pid), item_for(packet())))
    store.close()


def test_account_wide_quota_skips_peer_keys(tmp_path):
    store, _ = queued_store(tmp_path)
    adapter = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("quota", status=429, retry_after=60, provider_wide=True)]})
    slots = [Slot("groq", "GROQ_KEY", "fake"), Slot("groq", "GROQ_KEY_2", "fake"),
             Slot("mistral", "MISTRAL_API", "fake")]
    assert phase5.process_next(store, route(store, adapter, slots), MODELS) == "COMPLETE"
    assert [name for _, name, _, _ in adapter.calls] == ["GROQ_KEY", "MISTRAL_API"]
    store.close()


def test_disagreement_remains_unresolved_after_deep_review(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    assert phase5.process_next(store, route(store, ScenarioAdapter()), MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, route(store, ScenarioAdapter(review="POSSIBLE_ESTIMATOR_ERROR")), MODELS) == "COMPLETE"
    assert store.db.execute("SELECT escalation_reason FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "DISAGREEMENT"
    phase5.queue_review_batches(store)
    deep_adapter = ScenarioAdapter(deep="POSSIBLE_ESTIMATOR_ERROR")
    strong = "x-free-strong"
    deep = Router(store, route(store, deep_adapter).slots, adapter=deep_adapter, approved_free_routes=FREE | {("gemini", strong)})
    assert phase5.process_next(store, deep, {**MODELS, "deep:gemini": strong}) == "COMPLETE"
    assert store.db.execute("SELECT classification,status FROM ai_ops_findings").fetchone()[:] == ("ALERT_LIFECYCLE_ERROR", "INVESTIGATING")
    assert store.db.execute("SELECT escalation_reason FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "DETERMINISTIC_INVESTIGATION_REQUIRED"
    store.close()


def test_unapproved_strong_model_stays_durable_and_restart_resumes_only_deep(tmp_path):
    store, (pid,) = queued_store(tmp_path)
    adapter = ScenarioAdapter()
    reviewer = route(store, adapter)
    assert phase5.process_next(store, reviewer, MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, reviewer, MODELS) == "COMPLETE"
    phase5.queue_review_batches(store)
    assert phase5.process_next(store, reviewer, MODELS) == "PENDING_AI"
    store.close()
    resumed = Store(tmp_path)
    assert resumed.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 2
    assert resumed.db.execute("SELECT pending_role FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "deep_investigation"
    strong = "x-free-strong"
    approved = Router(resumed, reviewer.slots, adapter=ScenarioAdapter(), approved_free_routes=FREE | {("gemini", strong)})
    assert phase5.process_next(resumed, approved, {**MODELS, "deep:gemini": strong}) == "COMPLETE"
    assert resumed.db.execute("SELECT count(*) FROM ai_ops_reviews").fetchone()[0] == 3
    resumed.close()


def test_quality_summary_is_observational(tmp_path):
    store, _ = queued_store(tmp_path)
    reviewer = route(store, ScenarioAdapter())
    assert phase5.process_next(store, reviewer, MODELS) == "COMPLETE"
    summary = phase5.quality_summary(store)
    assert summary[0]["provider"] == "groq" and summary[0]["calls"] == 1
    # A malicious or simply poor observational statistic cannot enter Router.execute.
    store.db.execute("UPDATE ai_ops_usage SET success=0,malformed=1")
    assert reviewer.execute(phase5.AnalysisTask("triage", phase5._build_prompt("triage", [packet()])), MODELS).provider == "groq"
    store.close()


def test_schema_five_upgrade_preserves_prior_review_and_finding(tmp_path):
    store = Store(tmp_path)
    p = packet(); pid = add_packet(store, p)
    validated = phase5.validate_result(p, item_for(p))
    finding, _ = phase5.upsert_finding(store, pid, validated)
    phase5._persist_review(store, pid, "triage", "groq", "g-free", "GROQ_KEY", validated)
    # Recreate exactly the old schema surface on a disposable fixture, including
    # its canonical data. The real migration must not replace these rows.
    store.db.execute("DROP TABLE ai_ops_feedback")
    store.db.execute("ALTER TABLE ai_ops_reviews DROP COLUMN independence")
    store.db.execute("ALTER TABLE ai_ops_cases DROP COLUMN escalation_reason")
    store.db.execute("PRAGMA user_version=5")
    store.close()
    upgraded = Store(tmp_path)
    assert upgraded.health()["schema"] == 6
    assert upgraded.db.execute("SELECT finding_id FROM ai_ops_findings").fetchone()[0] == finding
    assert upgraded.db.execute("SELECT packet_id,independence FROM ai_ops_reviews").fetchone()[:] == (pid, "NOT_APPLICABLE")
    phase5.record_feedback(upgraded, finding, "USEFUL")
    upgraded.close()


def test_generic_status_transition_cannot_bypass_resolution_proof(tmp_path):
    store = Store(tmp_path)
    p = packet(); pid = add_packet(store, p)
    finding, _ = phase5.upsert_finding(store, pid, phase5.validate_result(p, item_for(p)))
    phase5.transition_finding(store, finding, "INVESTIGATING")
    phase5.transition_finding(store, finding, "FIX_CANDIDATE")
    with pytest.raises(ValueError, match="verified resolution"):
        phase5.transition_finding(store, finding, "RESOLVED")
    phase5.transition_finding(store, finding, "DEPLOYED_PENDING_VERIFICATION")
    with pytest.raises(ValueError, match="verified resolution"):
        phase5.transition_finding(store, finding, "RESOLVED")
    assert store.db.execute("SELECT status FROM ai_ops_findings").fetchone()[0] == "DEPLOYED_PENDING_VERIFICATION"
    store.close()


@pytest.mark.parametrize("verdict", sorted(phase5.FEEDBACK))
def test_feedback_and_review_relationship_survive_dr(tmp_path, verdict):
    root = tmp_path / "root"; restored_root = tmp_path / "restore"
    root.mkdir(); restored_root.mkdir()
    store = Store(root)
    p = packet(); pid = add_packet(store, p)
    validated = phase5.validate_result(p, item_for(p))
    finding, _ = phase5.upsert_finding(store, pid, validated)
    phase5._persist_review(store, pid, "triage", "groq", "g-free", "GROQ_KEY", validated)
    phase5._persist_review(store, pid, "independent_review", "mistral", "m-free", "MISTRAL_API", validated)
    phase5.record_feedback(store, finding, verdict)
    data, manifest = export(store, source_commit="a" * 40)
    assert verdict.encode() in data
    store.close()
    restored = restore_empty(restored_root, data, manifest)
    assert restored.db.execute("SELECT verdict FROM ai_ops_feedback").fetchone()[0] == verdict
    assert restored.db.execute("SELECT independence FROM ai_ops_reviews WHERE role='independent_review'").fetchone()[0] == "DIFFERENT_PROVIDER_AND_MODEL_BLIND"
    restored.close()


def test_invented_replay_and_secret_echo_rejected(tmp_path):
    p = packet()
    invented = item_for(p); invented["rationale"] = "Replay passed without physical evidence."
    assert phase5.validate_result(p, invented).validation_status == "REJECTED"
    store, _ = queued_store(tmp_path)
    class Echo(ScenarioAdapter):
        def execute(self, slot, task, model, *, now):
            answer = super().execute(slot, task, model, now=now)
            answer.analysis["summary"] = "Authorization: Bearer secret-echo"
            return answer
    assert phase5.process_next(store, route(store, Echo()), MODELS) == "PENDING_AI"
    saved = store.db.execute("SELECT output_json FROM ai_ops_steps WHERE step_id='model_call'").fetchone()[0]
    assert "secret-echo" not in saved
    store.close()
