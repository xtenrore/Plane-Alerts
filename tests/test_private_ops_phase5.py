import copy
import hashlib
import json
from pathlib import Path

import pytest

from app.private_ops import phase5
from app.private_ops.phase5 import (
    build_handoff, process_next, queue_review_batches, queue_triage_batches,
    resolve_finding, seed_pending_cases, terminal_finding, upsert_finding,
    validate_result,
)
from app.private_ops.provider_adapters import Adapter, AnalysisResult, AnalysisTask, ProviderFailure, Slot
from app.private_ops.provider_router import NoFreeRoute, Router
from app.private_ops.store import Store

MODELS = {"groq": "g-free", "mistral": "m-free", "gemini": "x-free"}
FREE = {(p, m) for p, m in MODELS.items()}


def packet(case="CASE-1", coverage="good", reason="cancellation"):
    return {"schema": 1, "window_start": 1790500000, "case_ref": case,
            "evidence_refs": [f"{case}-evt-1", f"{case}-evt-2"], "event_count": 2,
            "reasons": [reason] + (["missing_coverage"] if coverage == "missing" else []),
            "coverage": coverage,
            "samples": [
                {"kind": "prediction", "state": "QUALIFIED", "cpa_km": 4.2, "observed_km": None},
                {"kind": "outcome", "state": "CANCELLED", "cpa_km": 18.7, "observed_km": 4.6},
            ]}


def add_packet(store, p):
    text = json.dumps(p, sort_keys=True, separators=(",", ":"))
    pid = hashlib.sha256(text.encode()).hexdigest()
    with store.transaction() as db:
        db.execute("INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created) VALUES(?,?,?,?,?,?,0)",
                   (pid, p["window_start"], p["case_ref"], p["reasons"][0], text, pid))
    return pid


def item_for(p, classification="ALERT_LIFECYCLE_ERROR", severity="HIGH", *, bad_event=False, bad_number=False, secret=False):
    return {"case_ref": p["case_ref"], "classification": classification, "severity": severity,
            "subsystem": "alert_lifecycle" if "ALERT" in classification else "prediction",
            "event_ids": ["does-not-exist"] if bad_event else [p["evidence_refs"][0]],
            "cpa_km": [999.0] if bad_number else [4.2], "observed_km": [4.6],
            "states": ["QUALIFIED", "CANCELLED"], "coverage": p["coverage"],
            "rationale": "Bearer SECRET" if secret else "Evidence-supported audit result.",
            "needs_review": classification in phase5.SUSPICIOUS or severity in ("HIGH", "CRITICAL")}


def parse_packets(task):
    return json.loads(task.prompt.split("\nEVIDENCE=", 1)[1])


class ScenarioAdapter:
    def __init__(self, *, failures=None, triage="ALERT_LIFECYCLE_ERROR", review=None, deep=None,
                 malformed_first=False, malformed_always=False):
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.triage, self.review, self.deep = triage, review or triage, deep or (review or triage)
        self.malformed_first, self.malformed_always = malformed_first, malformed_always
        self.calls = []

    def execute(self, slot, task, model, *, now):
        self.calls.append((slot.provider, slot.name, task.purpose, "repair" in task.prompt.lower()))
        failures = self.failures.get(slot.name, [])
        if failures:
            action = failures.pop(0)
            if isinstance(action, Exception):
                raise action
        if self.malformed_always or (self.malformed_first and len(self.calls) == 1):
            return AnalysisResult(slot.provider, slot.name, model, {"summary": "bad", "findings": [{"wrong": True}]}, 3, 2)
        ps = parse_packets(task)
        cls = self.triage if task.purpose == "triage" else self.review if task.purpose == "independent_review" else self.deep
        findings = [item_for(p, cls, "HIGH" if cls in phase5.SUSPICIOUS else "LOW") for p in ps]
        return AnalysisResult(slot.provider, slot.name, model, {"summary": "bounded", "findings": findings}, 10, 5)


def router(store, adapter, slots=None):
    slots = slots or [Slot("groq", "GROQ_KEY", "redacted"), Slot("mistral", "MISTRAL_API", "redacted"), Slot("gemini", "GEMINI_API_KEY", "redacted")]
    return Router(store, slots, adapter=adapter, approved_free_routes=FREE)


def queued_store(tmp_path, count=1, coverage="good"):
    s = Store(tmp_path)
    pids = [add_packet(s, packet(f"CASE-{i+1}", coverage)) for i in range(count)]
    seed_pending_cases(s)
    queue_triage_batches(s)
    return s, pids


# 1. Valid Groq triage.
def test_01_valid_groq_triage(tmp_path):
    s, pids = queued_store(tmp_path)
    a = ScenarioAdapter()
    assert process_next(s, router(s, a), MODELS) == "COMPLETE"
    row = s.db.execute("SELECT classification,state FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()
    assert tuple(row) == ("ALERT_LIFECYCLE_ERROR", "REVIEWING")
    assert a.calls[0][0] == "groq"
    s.close()


# 2. Bad Groq key -> next Groq key.
def test_02_groq_key_failure_fails_over_to_peer(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("auth")]})
    r = router(s, a, [Slot("groq", "GROQ_KEY", "a"), Slot("groq", "GROQ_KEY_2", "b")])
    assert process_next(s, r, MODELS) == "COMPLETE"
    assert [c[1] for c in a.calls[:2]] == ["GROQ_KEY", "GROQ_KEY_2"]
    s.close()


# 3. One Groq 429 does not mark all Groq exhausted.
def test_03_single_groq_quota_still_uses_remaining_pool(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("quota", status=429, retry_after=60, provider_wide=False)]})
    r = router(s, a, [Slot("groq", "GROQ_KEY", "a"), Slot("groq", "GROQ_KEY_2", "b"), Slot("mistral", "MISTRAL_API", "c")])
    assert process_next(s, r, MODELS) == "COMPLETE"
    assert [c[0] for c in a.calls[:2]] == ["groq", "groq"]
    assert r.health(now=1)[0]["state"] == "RATE_LIMITED"
    s.close()


# 4. All Groq unavailable -> compatible provider.
def test_04_all_groq_unavailable_cross_provider(tmp_path):
    s, _ = queued_store(tmp_path)
    failures = {f"GROQ_KEY{'' if i == 1 else '_' + str(i)}": [ProviderFailure("auth")] for i in range(1, 6)}
    slots = [Slot("groq", "GROQ_KEY" if i == 1 else f"GROQ_KEY_{i}", str(i)) for i in range(1, 6)] + [Slot("mistral", "MISTRAL_API", "m")]
    a = ScenarioAdapter(failures=failures)
    assert process_next(s, router(s, a, slots), MODELS) == "COMPLETE"
    assert a.calls[-1][0] == "mistral" and len(a.calls) == 6
    s.close()


# 5. Timeout is bounded and fails over.
def test_05_timeout_handled(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("timeout")]})
    assert process_next(s, router(s, a), MODELS) == "COMPLETE"
    assert s.db.execute("SELECT failure_kind FROM ai_ops_usage WHERE success=0").fetchone()[0] == "timeout"
    s.close()


# 6. 5xx/server failure.
def test_06_server_failure_handled(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("server", status=503)]})
    assert process_next(s, router(s, a), MODELS) == "COMPLETE"
    assert s.db.execute("SELECT failure_kind FROM ai_ops_usage WHERE success=0").fetchone()[0] == "server"
    s.close()


# 7. Auth failure.
def test_07_auth_failure_state(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(failures={"GROQ_KEY": [ProviderFailure("auth", status=401)]})
    r = router(s, a)
    assert process_next(s, r, MODELS) == "COMPLETE"
    assert next(x for x in r.health(now=1) if x["slot"] == "GROQ_KEY")["state"] == "AUTH_FAILED"
    s.close()


class FakeHTTP:
    def __init__(self, response): self.response = response
    def post(self, url, headers, body): return self.response
    def get_status(self, url, headers): return 200

# 8. Malformed JSON provider response is rejected without body leakage.
def test_08_malformed_json_rejected():
    with pytest.raises(ProviderFailure, match="malformed"):
        Adapter(FakeHTTP((200, {}, b"{not-json"))).execute(Slot("groq", "GROQ_KEY", "SECRET"), AnalysisTask("triage", "safe"), "g-free", now=0)


# 9. Exactly one structured repair attempt.
def test_09_one_bounded_repair_attempt(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(malformed_first=True)
    assert process_next(s, router(s, a), MODELS) == "COMPLETE"
    assert len(a.calls) == 2 and a.calls[1][3]
    assert s.db.execute("SELECT count(*) FROM ai_ops_steps WHERE step_id='repair_call'").fetchone()[0] == 1
    s.close()


# 10. Context-too-large becomes PENDING_AI, not unbounded retry/crash.
def test_10_context_too_large_pending(tmp_path):
    s, pids = queued_store(tmp_path)
    failures = {"GROQ_KEY": [ProviderFailure("context")], "MISTRAL_API": [ProviderFailure("context")], "GEMINI_API_KEY": [ProviderFailure("context")]}
    assert process_next(s, router(s, ScenarioAdapter(failures=failures)), MODELS) == "PENDING_AI"
    assert s.db.execute("SELECT state,last_error FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()[0] == "PENDING_AI"
    s.close()


# 11. All AI unavailable preserves job/evidence and PENDING_AI.
def test_11_all_ai_unavailable_preserves_pending(tmp_path):
    s, pids = queued_store(tmp_path)
    failures = {n: [ProviderFailure("auth")] for n in ("GROQ_KEY", "MISTRAL_API", "GEMINI_API_KEY")}
    assert process_next(s, router(s, ScenarioAdapter(failures=failures)), MODELS) == "PENDING_AI"
    assert s.db.execute("SELECT count(*) FROM ai_ops_evidence WHERE packet_id=?", (pids[0],)).fetchone()[0] == 1
    assert s.db.execute("SELECT status FROM ai_ops_jobs").fetchone()[0] == "RETRY"
    s.close()


# 12. Capacity later returns and backlog resumes from durable state.
def test_12_backlog_later_resumes(tmp_path):
    s, pids = queued_store(tmp_path)
    failures = {n: [ProviderFailure("auth")] for n in ("GROQ_KEY", "MISTRAL_API", "GEMINI_API_KEY")}
    assert process_next(s, router(s, ScenarioAdapter(failures=failures)), MODELS) == "PENDING_AI"
    # Cooldowns are a provider-health fact; simulate later time when cooldown has elapsed.
    s.db.execute("UPDATE ai_ops_provider_health SET cooldown_until=0")
    assert process_next(s, router(s, ScenarioAdapter()), MODELS, now=10000) == "COMPLETE"
    assert s.db.execute("SELECT state FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()[0] == "REVIEWING"
    s.close()


# 13. Restart after model batch call reuses checkpoint and resumes per-case commits.
def test_13_restart_mid_batch_reuses_model_checkpoint(tmp_path):
    s, pids = queued_store(tmp_path, count=2)
    bid = s.claim("old")
    row = s.db.execute("SELECT packet_ids_json FROM ai_ops_batches WHERE batch_id=?", (bid,)).fetchone()
    ps = [phase5._packet(s, p) for p in json.loads(row[0])]
    a = ScenarioAdapter()
    assert phase5._analysis_call(s, router(s, a), MODELS, batch_id=bid, role="triage", packet_ids=pids, packets=ps,
                                 worker="old", preferred=["groq"], exclude=[], repair=False, now=0)
    assert len(a.calls) == 1
    s.db.execute("UPDATE ai_ops_jobs SET lease_until=0 WHERE id=?", (bid,))
    s.db.execute("UPDATE ai_ops_steps SET lease_until=0 WHERE job_id=?", (bid,))
    s.close()
    s = Store(tmp_path)
    b = ScenarioAdapter()
    assert process_next(s, router(s, b), MODELS, worker="new", now=1000) == "COMPLETE"
    assert len(b.calls) == 0
    assert s.db.execute("SELECT count(*) FROM ai_ops_cases WHERE state='REVIEWING'").fetchone()[0] == 2
    s.close()


# 14. Completed cases are not re-triaged.
def test_14_completed_cases_not_repeated(tmp_path):
    s, _ = queued_store(tmp_path)
    a = ScenarioAdapter(triage="NORMAL_EXPECTED_CHANGE")
    assert process_next(s, router(s, a), MODELS) == "COMPLETE"
    assert queue_triage_batches(s) == []
    assert process_next(s, router(s, a), MODELS) is None
    assert len(a.calls) == 1
    s.close()


# 15. Incomplete current per-case step safely restarts.
def test_15_incomplete_case_step_restarts(tmp_path):
    s, pids = queued_store(tmp_path)
    bid = s.claim("old")
    ps = [phase5._packet(s, pids[0])]
    a = ScenarioAdapter()
    call = phase5._analysis_call(s, router(s, a), MODELS, batch_id=bid, role="triage", packet_ids=pids, packets=ps,
                                 worker="old", preferred=["groq"], exclude=[], repair=False, now=0)
    result = phase5._strict_batch({ps[0]["case_ref"]: ps[0]}, call[0])[0][0]
    marker = {"packet_id": pids[0], "role": "triage", "result": result.record()}
    assert s.begin_step(bid, "case:" + pids[0][:48], "old", marker)
    s.db.execute("UPDATE ai_ops_jobs SET lease_until=0 WHERE id=?", (bid,))
    s.db.execute("UPDATE ai_ops_steps SET lease_until=0 WHERE job_id=?", (bid,))
    assert process_next(s, router(s, ScenarioAdapter()), MODELS, worker="new", now=1000) == "COMPLETE"
    assert s.db.execute("SELECT state FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()[0] == "REVIEWING"
    s.close()


# 16. Independent reviewer agrees and uses a different provider.
def test_16_independent_reviewer_agrees(tmp_path):
    s, pids = queued_store(tmp_path)
    a = ScenarioAdapter()
    r = router(s, a)
    assert process_next(s, r, MODELS) == "COMPLETE"
    queue_review_batches(s)
    assert process_next(s, r, MODELS) == "COMPLETE"
    reviews = s.db.execute("SELECT role,provider,agreement FROM ai_ops_reviews WHERE packet_id=? ORDER BY created", (pids[0],)).fetchall()
    assert reviews[0][1] == "groq" and reviews[1][1] == "mistral" and reviews[1][2] == "AGREE"
    s.close()


# 17. Independent reviewer disagreement is recorded.
def test_17_independent_reviewer_disagrees(tmp_path):
    s, pids = queued_store(tmp_path)
    a1 = ScenarioAdapter(triage="ALERT_LIFECYCLE_ERROR")
    assert process_next(s, router(s, a1), MODELS) == "COMPLETE"
    queue_review_batches(s)
    a2 = ScenarioAdapter(review="POSSIBLE_ESTIMATOR_ERROR")
    assert process_next(s, router(s, a2), MODELS) == "COMPLETE"
    assert s.db.execute("SELECT state FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()[0] == "DEEP_REVIEW"
    s.close()


# 18. Disagreement escalates to deep Gemini review.
def test_18_disagreement_escalates_to_deep_reviewer(tmp_path):
    s, pids = queued_store(tmp_path)
    assert process_next(s, router(s, ScenarioAdapter(triage="ALERT_LIFECYCLE_ERROR")), MODELS) == "COMPLETE"
    queue_review_batches(s)
    assert process_next(s, router(s, ScenarioAdapter(review="POSSIBLE_ESTIMATOR_ERROR")), MODELS) == "COMPLETE"
    queue_review_batches(s)
    a3 = ScenarioAdapter(deep="ALERT_LIFECYCLE_ERROR")
    strong = "x-free-strong"
    deep_router = Router(s, [Slot("gemini", "GEMINI_API_KEY", "fake")], adapter=a3,
                         approved_free_routes=FREE | {("gemini", strong)})
    assert process_next(s, deep_router, {**MODELS, "deep:gemini": strong}) == "COMPLETE"
    deep = s.db.execute("SELECT provider,role FROM ai_ops_reviews WHERE packet_id=? AND role='deep_investigation'", (pids[0],)).fetchone()
    assert tuple(deep) == ("gemini", "deep_investigation")
    s.close()


# 19. Hallucinated event ID is rejected.
def test_19_hallucinated_event_id_rejected():
    p = packet()
    r = validate_result(p, item_for(p, bad_event=True))
    assert r.validation_status == "REJECTED" and r.classification == "INSUFFICIENT_EVIDENCE" and "event_id" in r.validation_errors


# 20. Wrong numeric evidence is rejected.
def test_20_wrong_numeric_evidence_rejected():
    p = packet()
    r = validate_result(p, item_for(p, bad_number=True))
    assert r.validation_status == "REJECTED" and "cpa_km" in r.validation_errors


# 21. Missing ADS-B coverage remains inconclusive.
def test_21_missing_coverage_downgraded():
    p = packet(coverage="missing")
    r = validate_result(p, item_for(p, "POSSIBLE_ESTIMATOR_ERROR"))
    assert r.classification == "COVERAGE_INCONCLUSIVE" and r.validation_status == "DOWNGRADED"


# 22. Duplicate active finding links evidence instead of multiplying cases.
def test_22_finding_deduplication(tmp_path):
    s = Store(tmp_path)
    p1 = packet("SAME")
    pid1 = add_packet(s, p1)
    pid2 = add_packet(s, {**p1, "window_start": p1["window_start"] + 3600})
    v = validate_result(p1, item_for(p1))
    f1, d1 = upsert_finding(s, pid1, v)
    # second packet has same signature fields despite different immutable packet id
    p2 = phase5._packet(s, pid2)
    v2 = validate_result(p2, item_for(p2))
    f2, d2 = upsert_finding(s, pid2, v2)
    assert f1 == f2 and d1 == "CREATED" and d2 == "DEDUPED_ACTIVE"
    assert s.db.execute("SELECT count(*) FROM ai_ops_findings").fetchone()[0] == 1
    s.close()


def setup_resolvable(s):
    p = packet("RESOLVE")
    pid = add_packet(s, p)
    seed_pending_cases(s)
    v = validate_result(p, item_for(p))
    fid, _ = upsert_finding(s, pid, v)
    s.db.execute("UPDATE ai_ops_cases SET finding_id=?,state='INVESTIGATING' WHERE packet_id=?", (fid, pid))
    return pid, fid, v


# 23. Resolved finding disappears from active queue.
def test_23_resolved_removed_from_active(tmp_path):
    s = Store(tmp_path); pid, fid, _ = setup_resolvable(s)
    resolve_finding(s, fid, fix_commit="a"*40, fix_version="5.8.8", tests_passed=True, ci_passed=True,
                    deployment_required=True, deployed=True, production_verified=True, replay_summary="Replay passed")
    assert build_handoff(s)["unresolved_findings"] == []
    assert s.db.execute("SELECT state FROM ai_ops_cases WHERE packet_id=?", (pid,)).fetchone()[0] == "RESOLVED"
    s.close()


# 24. Resolved finding remains historical with fix evidence.
def test_24_resolved_history_preserved(tmp_path):
    s = Store(tmp_path); _, fid, _ = setup_resolvable(s)
    resolve_finding(s, fid, fix_commit="b"*40, fix_version="5.8.8", tests_passed=True, ci_passed=True,
                    deployment_required=False, deployed=False, production_verified=True, replay_summary="Regression replay passed", error_museum_ref="EM-1")
    row = s.db.execute("SELECT status,fix_commit,resolution_json FROM ai_ops_findings WHERE finding_id=?", (fid,)).fetchone()
    assert row[0] == "RESOLVED" and row[1] == "b"*40 and "EM-1" in row[2]
    s.close()


# 25. Resolved finding is absent from unresolved handoff but bounded recent reference remains.
def test_25_handoff_cleanup(tmp_path):
    s = Store(tmp_path); _, fid, _ = setup_resolvable(s)
    resolve_finding(s, fid, fix_commit="c"*40, fix_version="5.8.8", tests_passed=True, ci_passed=True,
                    deployment_required=False, deployed=False, production_verified=True, replay_summary="ok")
    h = build_handoff(s)
    assert h["unresolved_findings"] == [] and h["recent_resolved"][0]["finding_id"] == fid
    s.close()


# 26. Same post-fix signature becomes linked regression occurrence.
def test_26_post_fix_signature_creates_linked_regression(tmp_path):
    s = Store(tmp_path); _, fid, v = setup_resolvable(s)
    resolve_finding(s, fid, fix_commit="d"*40, fix_version="5.8.8", tests_passed=True, ci_passed=True,
                    deployment_required=False, deployed=False, production_verified=True, replay_summary="ok")
    p2 = packet("RESOLVE"); p2["window_start"] += 3600
    pid2 = add_packet(s, p2)
    v2 = validate_result(p2, item_for(p2))
    fid2, disposition = upsert_finding(s, pid2, v2)
    row = s.db.execute("SELECT previous_finding_id,occurrence,status FROM ai_ops_findings WHERE finding_id=?", (fid2,)).fetchone()
    assert disposition == "REGRESSION_OCCURRENCE" and tuple(row) == (fid, 2, "TRIAGED")
    s.close()


# 27. Secrets are rejected from prompts/results and never persisted.
def test_27_secret_redaction_gate(tmp_path):
    p = packet(); bad = item_for(p, secret=True)
    result = validate_result(p, bad)
    assert result.validation_status == "REJECTED" and "SECRET" not in json.dumps(result.record())
    with pytest.raises(ValueError, match="sanitized prompt"):
        phase5._build_prompt("triage", [{**p, "leak": "Bearer TOPSECRET"}])


# 28. Model path cannot mutate deterministic flight state.
def test_28_analysis_isolation_no_flight_mutation(tmp_path):
    s, pids = queued_store(tmp_path)
    before = copy.deepcopy(phase5._packet(s, pids[0]))
    assert process_next(s, router(s, ScenarioAdapter()), MODELS) == "COMPLETE"
    assert phase5._packet(s, pids[0]) == before
    source = Path(phase5.__file__).read_text()
    assert "app.main" not in source and "app.aircraft" not in source and "flight_state" not in source
    s.close()


# 29. AI Ops capacity failure remains confined to its store/service boundary.
def test_29_ai_ops_failure_isolated(tmp_path):
    s, pids = queued_store(tmp_path)
    failures = {n: [ProviderFailure("server", status=503)] for n in ("GROQ_KEY", "MISTRAL_API", "GEMINI_API_KEY")}
    assert process_next(s, router(s, ScenarioAdapter(failures=failures)), MODELS) == "PENDING_AI"
    assert s.health()["integrity"] == "ok" and s.db.execute("SELECT count(*) FROM ai_ops_evidence").fetchone()[0] == 1
    s.close()


# 30. Phase-5 module has no timer/scheduler loop and therefore cannot lengthen the 5-second path.
def test_30_no_monitor_scheduler_or_hot_path_dependency():
    source = Path(phase5.__file__).read_text()
    assert "serve_forever" not in source and "sleep(" not in source and "monitor_timing" not in source
    assert "trajectory" not in source.split("from .provider_adapters", 1)[0]


def test_resolution_gates_refuse_unverified_fix(tmp_path):
    s = Store(tmp_path); _, fid, _ = setup_resolvable(s)
    with pytest.raises(ValueError, match="gates"):
        resolve_finding(s, fid, fix_commit="e"*40, fix_version="5.8.8", tests_passed=True, ci_passed=False,
                        deployment_required=True, deployed=False, production_verified=False, replay_summary="not verified")
    s.close()


def test_31_backlog_capacity_failure_does_not_spin_or_duplicate_batch(tmp_path):
    s, pids = queued_store(tmp_path)
    failures = {n: [ProviderFailure("auth")] for n in ("GROQ_KEY", "MISTRAL_API", "GEMINI_API_KEY")}
    a = ScenarioAdapter(failures=failures)
    result = phase5.process_backlog(s, router(s, a), MODELS, max_batches=64)
    assert result == {"complete": 0, "pending_ai": 1}
    assert len(a.calls) == 3
    assert s.db.execute("SELECT count(*) FROM ai_ops_batches").fetchone()[0] == 1
    row = s.db.execute("SELECT state,batch_id,retry_count FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()
    assert row[0] == "PENDING_AI" and row[1] is not None and row[2] == 1
    s.close()


def test_32_repeated_malformed_output_has_bounded_terminal_fallback(tmp_path):
    s, pids = queued_store(tmp_path)
    for _ in range(phase5.MAX_MALFORMED_BATCH_RETRIES):
        result = phase5.process_backlog(s, router(s, ScenarioAdapter(malformed_always=True)), MODELS, max_batches=8)
        assert result["pending_ai"] == 1
    row = s.db.execute("SELECT state,classification,retry_count FROM ai_ops_cases WHERE packet_id=?", (pids[0],)).fetchone()
    assert tuple(row) == ("INCONCLUSIVE", "INSUFFICIENT_EVIDENCE", phase5.MAX_MALFORMED_BATCH_RETRIES)
    assert phase5.queue_triage_batches(s) == []
    s.close()


def test_33_unsupported_freeform_claim_is_rejected_even_when_structured_citations_exist():
    p = packet()
    bad = item_for(p)
    bad["rationale"] = "ETA was 120 seconds according to this event."
    result = validate_result(p, bad)
    assert result.validation_status == "REJECTED"
    assert "unsupported_rationale" in result.validation_errors


def test_34_batching_splits_on_serialized_prompt_bound(tmp_path, monkeypatch):
    s = Store(tmp_path)
    pids = []
    for i in range(3):
        p = packet(f"B{i}")
        p["case_ref"] = f"CASE{i}"
        p["evidence_refs"] = [f"EVENT{i}"]
        pids.append(add_packet(s, p))
    phase5.seed_pending_cases(s)
    single_size = len(phase5._build_prompt("triage", [phase5._packet(s, pids[0])]).encode())
    monkeypatch.setattr(phase5, "MAX_PROMPT_BYTES", single_size + 16)
    batches = phase5.queue_triage_batches(s, max_cases=3)
    assert len(batches) == 3
    assigned = [json.loads(s.db.execute("SELECT packet_ids_json FROM ai_ops_batches WHERE batch_id=?", (bid,)).fetchone()[0]) for bid in batches]
    assert assigned == [[pid] for pid in sorted(pids)]
    s.close()
