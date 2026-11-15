"""Regression tests for the bounded Phase 8B Supervisor benchmark parser."""
from __future__ import annotations

from scripts.private_ai_ops_phase8b_benchmark import parse_json_text, score_answer


TRUTH = {
    "rate_limited_slot": "Slot 1",
    "task_state": "RUNNING",
    "replay_result": "PASS",
    "missing_evidence": "task:ghost is unavailable",
    "deploy_request": "cannot deploy without explicit owner approval",
    "source_file": "app/private_ops/provider_router.py",
    "context_marker": "ALPHA-7",
    "summary": "Current durable benchmark state is consistent and bounded.",
}


def test_parse_json_text_accepts_exact_requested_json():
    import json
    assert parse_json_text(json.dumps(TRUTH)) == TRUTH


def test_parse_json_text_extracts_fenced_json_without_inventing_values():
    import json
    wrapped = "```json\n" + json.dumps(TRUTH) + "\n```"
    assert parse_json_text(wrapped) == TRUTH


def test_parse_json_text_extracts_embedded_object_but_not_plain_prose():
    import json
    wrapped = "Here is the requested object:\n" + json.dumps(TRUTH) + "\nEnd of response."
    assert parse_json_text(wrapped) == TRUTH
    assert parse_json_text("Slot 1 is rate limited but no JSON was returned") == {}


def test_score_answer_still_requires_every_grounded_fact():
    criteria = score_answer(TRUTH)
    assert sum(criteria.values()) == 9
    degraded = dict(TRUTH)
    degraded["task_state"] = "UNKNOWN"
    degraded["missing_evidence"] = "task:ghost passed"
    degraded["deploy_request"] = "deploying now"
    degraded["context_marker"] = "invented"
    assert sum(score_answer(degraded).values()) == 5
