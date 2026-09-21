from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import prediction_lab_audit
from app.shadow_evaluation_v52 import (
    evaluate_live_outcome,
    evaluate_replay_cases,
    evaluate_snapshot_outcome,
    feature_flags,
    models_for_snapshot,
    promotion_assessment,
    summarize_evaluations,
)
from app.version import PREDICTION_VERSION, VERSION


FIXTURE = Path(__file__).parent / "fixtures" / "v52_shadow_replay.json"


def _cases():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_v52_identity_keeps_physical_predictor_unchanged():
    major, minor, patch = (int(part) for part in VERSION.split("."))
    assert (major, minor, patch) >= (5, 2, 0)
    assert PREDICTION_VERSION == "5.1-3d-proximity"


def test_shadow_feature_flags_default_to_shadow_only_enabled(monkeypatch):
    for name in (
        "PLANE_SHADOW_EVALUATION_ENABLED",
        "PLANE_SHADOW_V46_LINEAR_ENABLED",
        "PLANE_SHADOW_V46_TURN_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    assert feature_flags() == {
        "evaluation_enabled": True,
        "v46_linear_enabled": True,
        "v46_turn_enabled": True,
    }


def test_shadow_feature_flags_can_disable_individual_candidates(monkeypatch):
    monkeypatch.setenv("PLANE_SHADOW_EVALUATION_ENABLED", "true")
    monkeypatch.setenv("PLANE_SHADOW_V46_LINEAR_ENABLED", "false")
    monkeypatch.setenv("PLANE_SHADOW_V46_TURN_ENABLED", "true")
    assert feature_flags() == {
        "evaluation_enabled": True,
        "v46_linear_enabled": False,
        "v46_turn_enabled": True,
    }


def test_models_for_snapshot_uses_captured_feature_state_not_current_environment(monkeypatch):
    snapshot = {
        "shadow_feature_flags": {
            "evaluation_enabled": True,
            "v46_linear_enabled": True,
            "v46_turn_enabled": False,
        }
    }
    monkeypatch.setenv("PLANE_SHADOW_V46_LINEAR_ENABLED", "false")
    monkeypatch.setenv("PLANE_SHADOW_V46_TURN_ENABLED", "true")
    assert models_for_snapshot(snapshot) == ["production_control", "v46_linear"]


def test_pre_v52_snapshot_is_explicitly_unversioned_not_current_release():
    snapshot = {}
    result = evaluate_snapshot_outcome(
        snapshot,
        {"outcome": "passed", "observed_closest_km": 4.0},
    )
    assert result["release_version"] == "pre-v5.2-unversioned"


def test_observed_pass_scores_cpa_eta_false_negative_lead_time_and_calibration():
    case = next(case for case in _cases() if case["name"] == "turning_pass")
    evaluation = evaluate_replay_cases([case])[0]
    control = evaluation["models"]["production_control"]
    turn = evaluation["models"]["v46_turn"]

    assert control["cpa_absolute_error_km"] == pytest.approx(4.8)
    assert control["eta_absolute_error_s"] == pytest.approx(31.0)
    assert control["false_negative"] is True
    assert control["classification_correct"] is False
    assert control["alert_lead_time_s"] is None
    assert control["confidence_brier"] == pytest.approx(0.65**2)
    assert turn["false_negative"] is False
    assert turn["classification_correct"] is True


def test_unresolved_cancel_is_not_counted_as_successful_negative_truth():
    case = next(case for case in _cases() if case["name"] == "unresolved_cancel")
    evaluation = evaluate_replay_cases([case])[0]
    assert evaluation["truth"]["scoreable"] is False
    assert evaluation["truth"]["classification"] is None
    assert evaluation["models"]["production_control"]["classification_correct"] is None
    assert evaluation["models"]["production_control"]["false_positive"] is None


def test_missing_coverage_is_unresolved():
    case = next(case for case in _cases() if case["name"] == "missing_coverage")
    evaluation = evaluate_replay_cases([case])[0]
    assert evaluation["truth"]["scoreable"] is False
    assert evaluation["truth"]["reason"] == "missing_adsb_coverage"


def test_replay_summary_preserves_missing_denominators():
    summary = summarize_evaluations(evaluate_replay_cases(_cases()))
    control = summary["production_control"]
    assert control["scoreable_samples"] == 2
    assert control["positive_samples"] == 2
    assert control["negative_samples"] == 0
    assert control["false_negative_count"] == 1
    assert control["false_positive_rate"] is None
    assert control["cancellation_accuracy"] is None


def test_promotion_assessment_never_auto_promotes():
    summary = summarize_evaluations(evaluate_replay_cases(_cases()))
    result = promotion_assessment(summary, "v46_turn")
    assert result["automatic_promotion"] is False
    assert result["eligible_for_engineering_review"] is False
    assert result["blockers"]


def test_live_outcome_prefers_physical_closest_timestamp(monkeypatch):
    captured = {}

    async def fake_find_one(query, sort=None):
        assert query["user_id"] == 42
        return {
            "_id": "snap-1",
            "user_id": 42,
            "aircraft_icao24": "abc123",
            "captured_at": prediction_lab_audit._utc_from_timestamp(1000.0),
            "control": {"projected_closest_km": 2.0, "time_to_cpa_s": 100.0, "confidence_score": 0.8, "qualifies": True},
            "shadow": {},
            "release_version": VERSION,
            "shadow_feature_flags": feature_flags(),
        }

    class FakeCollection:
        async def find_one(self, query, sort=None):
            return await fake_find_one(query, sort)

        async def update_one(self, query, update, upsert=False):
            captured["document"] = update["$set"]

    monkeypatch.setattr(prediction_lab_audit, "_collection", lambda name: FakeCollection())
    monkeypatch.setattr(prediction_lab_audit, "_closest_observation_timestamp", lambda *args, **kwargs: 1095.0)

    outcome = SimpleNamespace(
        user_id=42,
        aircraft_icao24="abc123",
        outcome="passed",
        observed_closest_km=1.5,
        resolved_at=prediction_lab_audit._utc_from_timestamp(1110.0),
        route_suppressed=False,
        route_reason="",
        previous_projected_closest_km=None,
    )

    import asyncio

    asyncio.run(evaluate_live_outcome(outcome))
    document = captured["document"]
    assert document["truth"]["eta_truth_basis"] == "observed_closest_timestamp"
    assert document["truth"]["actual_closest_timestamp"] == 1095.0


def test_live_outcome_without_physical_closest_timestamp_leaves_eta_unavailable(monkeypatch):
    captured = {}

    async def fake_find_one(query, sort=None):
        return {
            "_id": "snap-2",
            "user_id": 42,
            "aircraft_icao24": "abc123",
            "captured_at": prediction_lab_audit._utc_from_timestamp(1000.0),
            "control": {"projected_closest_km": 2.0, "time_to_cpa_s": 100.0, "confidence_score": 0.8, "qualifies": True},
            "shadow": {},
            "release_version": VERSION,
            "shadow_feature_flags": feature_flags(),
        }

    class FakeCollection:
        async def find_one(self, query, sort=None):
            return await fake_find_one(query, sort)

        async def update_one(self, query, update, upsert=False):
            captured["document"] = update["$set"]

    monkeypatch.setattr(prediction_lab_audit, "_collection", lambda name: FakeCollection())
    monkeypatch.setattr(prediction_lab_audit, "_closest_observation_timestamp", lambda *args, **kwargs: None)

    outcome = SimpleNamespace(
        user_id=42,
        aircraft_icao24="abc123",
        outcome="passed",
        observed_closest_km=1.5,
        resolved_at=prediction_lab_audit._utc_from_timestamp(1110.0),
        route_suppressed=False,
        route_reason="",
        previous_projected_closest_km=None,
    )

    import asyncio

    asyncio.run(evaluate_live_outcome(outcome))
    document = captured["document"]
    assert document["truth"]["eta_truth_basis"] == "unavailable"
    assert document["truth"]["actual_closest_timestamp"] is None
    assert document["models"]["production_control"]["eta_absolute_error_s"] is None
