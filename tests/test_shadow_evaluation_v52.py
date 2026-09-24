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


def test_v52_identity_keeps_historical_predictor_and_v53_advances_current_identity():
    major, minor, patch = (int(part) for part in VERSION.split("."))
    assert (major, minor, patch) >= (5, 2, 0)
    if (major, minor) >= (5, 3):
        assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"
    else:
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


def test_snapshot_exposes_control_and_two_candidates_without_promotion():
    snapshot = _cases()[0]["snapshot"]
    models = models_for_snapshot(snapshot)
    assert [item["role"] for item in models] == [
        "production-control",
        "shadow-candidate",
        "shadow-candidate",
    ]
    assert models[0]["model_id"] == "5.1-3d-proximity"
    assert all("promote" not in item for item in models)


def test_snapshot_feature_flags_are_historical_not_current(monkeypatch):
    monkeypatch.setenv("PLANE_SHADOW_V46_TURN_ENABLED", "true")
    snapshot = dict(_cases()[0]["snapshot"])
    snapshot["shadow_feature_flags"] = {
        "evaluation_enabled": True,
        "v46_linear_enabled": True,
        "v46_turn_enabled": False,
    }
    models = models_for_snapshot(snapshot)
    assert [item["role"] for item in models] == ["production-control", "shadow-candidate"]
    assert models[1]["model_id"].endswith(":linear")


def test_observed_pass_scores_cpa_eta_false_negative_lead_time_and_calibration():
    case = _cases()[1]
    rows = evaluate_snapshot_outcome(case["snapshot"], case["outcome"])
    assert len(rows) == 3
    control = next(row for row in rows if row["model_role"] == "production-control")
    turn = next(row for row in rows if row["model_id"].endswith(":turn-aware"))
    assert control["cpa_error_km"] == pytest.approx(6.0)
    assert control["eta_error_s"] == pytest.approx(120.0)
    assert control["actual_eta_basis"] == "observed_closest_at"
    assert control["false_negative"] is True
    assert control["classification_correct"] is False
    assert control["alert_lead_time_s"] is None
    assert control["confidence_brier"] == pytest.approx(0.65**2)
    assert turn["false_negative"] is False
    assert turn["classification_correct"] is True
    assert turn["alert_lead_time_s"] == pytest.approx(600.0)
    assert turn["automatic_promotion_allowed"] is False


def test_explicit_observed_closest_timestamp_is_preferred_for_eta_truth():
    case = _cases()[0]
    outcome = dict(case["outcome"])
    outcome["observed_closest_at"] = "2026-09-21T05:01:50+00:00"
    rows = evaluate_snapshot_outcome(case["snapshot"], outcome)
    control = next(row for row in rows if row["model_role"] == "production-control")
    assert control["actual_eta_s"] == pytest.approx(110.0)
    assert control["actual_eta_basis"] == "observed_closest_at"
    assert control["eta_error_s"] == pytest.approx(10.0)


def test_unresolved_cancellation_and_missing_coverage_are_not_scored():
    cases = _cases()
    assert evaluate_snapshot_outcome(cases[2]["snapshot"], cases[2]["outcome"]) == []
    assert evaluate_snapshot_outcome(cases[3]["snapshot"], cases[3]["outcome"]) == []


def test_replay_summary_preserves_missing_denominators():
    result = evaluate_replay_cases(_cases())
    assert result["input_cases"] == 4
    assert result["evaluation_rows"] == 6
    assert result["skipped_unscoreable_cases"] == 2
    assert len(result["models"]) == 3
    control = next(item for item in result["models"] if item["model_role"] == "production-control")
    assert control["samples"] == 2
    assert control["positive_samples"] == 2
    assert control["negative_samples"] == 0
    assert control["false_negative_count"] == 1
    assert control["false_positive_rate"] is None
    assert control["cancellation_accuracy"] is None


def test_release_to_release_grouping_is_explicit():
    rows = evaluate_snapshot_outcome(_cases()[0]["snapshot"], _cases()[0]["outcome"])
    copied = [{**row, "release_version": "5.1.3"} for row in rows]
    summary = summarize_evaluations(rows + copied)
    assert {item["release_version"] for item in summary["models"]} == {"5.1.3", "5.2.0"}


def test_pre_v52_snapshot_is_not_mislabeled_as_current_release():
    snapshot = dict(_cases()[0]["snapshot"])
    snapshot.pop("release_version", None)
    rows = evaluate_snapshot_outcome(snapshot, _cases()[0]["outcome"])
    assert {row["release_version"] for row in rows} == {"pre-v5.2-unversioned"}


def test_promotion_never_automatic_even_when_every_gate_passes():
    control = {
        "samples": 100,
        "horizon_counts": {"0-10m": 50, "10-30m": 50},
        "false_positive_rate": 0.10,
        "false_negative_rate": 0.10,
    }
    candidate = {
        "samples": 100,
        "horizon_counts": {"0-10m": 50, "10-30m": 50},
        "false_positive_rate": 0.08,
        "false_negative_rate": 0.09,
    }
    assessment = promotion_assessment(
        candidate,
        control,
        replay_success=True,
        live_shadow_success=True,
    )
    assert assessment["eligible_for_manual_review"] is True
    assert assessment["automatic_promotion"] is False
    assert assessment["decision"] == "hold-for-human-review"


def test_promotion_holds_when_safety_denominator_is_missing():
    control = {
        "samples": 100,
        "horizon_counts": {"0-10m": 50, "10-30m": 50},
        "false_positive_rate": None,
        "false_negative_rate": 0.10,
    }
    candidate = {
        "samples": 100,
        "horizon_counts": {"0-10m": 50, "10-30m": 50},
        "false_positive_rate": None,
        "false_negative_rate": 0.08,
    }
    assessment = promotion_assessment(candidate, control, replay_success=True, live_shadow_success=True)
    assert assessment["requirements"]["safety_rates_comparable"] is False
    assert assessment["requirements"]["no_major_safety_regression"] is False
    assert assessment["eligible_for_manual_review"] is False
    assert assessment["automatic_promotion"] is False


def test_promotion_holds_on_sample_coverage_or_safety_regression():
    control = {
        "samples": 100,
        "horizon_counts": {"0-10m": 50, "10-30m": 50},
        "false_positive_rate": 0.05,
        "false_negative_rate": 0.05,
    }
    candidate = {
        "samples": 20,
        "horizon_counts": {"0-10m": 20},
        "false_positive_rate": 0.09,
        "false_negative_rate": 0.04,
    }
    assessment = promotion_assessment(candidate, control, replay_success=True, live_shadow_success=True)
    assert assessment["requirements"]["enough_samples"] is False
    assert assessment["requirements"]["representative_coverage"] is False
    assert assessment["requirements"]["no_major_safety_regression"] is False
    assert assessment["eligible_for_manual_review"] is False
    assert assessment["automatic_promotion"] is False


@pytest.mark.asyncio
async def test_live_evaluation_uses_one_bounded_bulk_write():
    snapshot = dict(_cases()[0]["snapshot"])
    outcome = dict(_cases()[0]["outcome"])
    outcome.update({"user_id": 1, "aircraft_icao24": "abc001"})

    class _Cursor:
        def __init__(self, rows):
            self.rows = rows

        def sort(self, *_args):
            return self

        def limit(self, value):
            self.rows = self.rows[:value]
            return self

        def __aiter__(self):
            self._iter = iter(self.rows)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class _AuditCollection:
        def find(self, _query):
            return _Cursor([snapshot])

    class _EvalCollection:
        def __init__(self):
            self.calls = []

        async def bulk_write(self, operations, ordered=False):
            self.calls.append((list(operations), ordered))
            return SimpleNamespace()

    evaluation = _EvalCollection()

    class _DB:
        def __getitem__(self, name):
            return _AuditCollection() if name == "prediction_lab_audit" else evaluation

    writes = await evaluate_live_outcome(_DB(), outcome)
    assert writes == 3
    assert len(evaluation.calls) == 1
    operations, ordered = evaluation.calls[0]
    assert len(operations) == 3
    assert ordered is False


@pytest.mark.asyncio
async def test_prediction_lab_marks_pass_scoreable_but_cancellation_unresolved(monkeypatch, tmp_path):
    written = []

    async def _append(doc, **_kwargs):
        written.append(dict(doc))
        return tmp_path / "evidence.json"

    monkeypatch.setattr(prediction_lab_audit, "append_evidence", _append)
    monkeypatch.setattr(prediction_lab_audit, "migration_verified", lambda: True)
    aircraft = SimpleNamespace(icao24="abc123", callsign="TEST1", aircraft_type="A320")
    prediction = SimpleNamespace(
        projected_closest_km=4.0,
        projected_closest_3d_km=4.2,
        projected_closest_3d_lower_bound_km=4.0,
        time_to_cpa_s=0.0,
        time_to_3d_cpa_s=0.0,
        state="Passed",
        confidence="High",
        altitude_relevance_applied=False,
    )
    await prediction_lab_audit.record_prediction_outcome(user_id=1, aircraft=aircraft, outcome="passed", observed_closest_km=4.1, final_prediction=prediction)
    passed = next(doc for doc in written if doc.get("kind") == "outcome")
    assert passed["scoreable"] is True
    assert passed["outcome_basis"] == "observed_in_radius_pass"

    written.clear()
    await prediction_lab_audit.record_prediction_outcome(user_id=1, aircraft=aircraft, outcome="cancelled", observed_closest_km=12.0, final_prediction=prediction)
    cancelled = next(doc for doc in written if doc.get("kind") == "outcome")
    assert cancelled["scoreable"] is False
    assert cancelled["outcome_basis"] == "lifecycle_transition_only"


def test_database_retires_high_volume_prediction_lab_indexes():
    source = Path("app/database.py").read_text(encoding="utf-8")
    assert 'db["prediction_lab_audit"].create_index' not in source
    assert 'db["prediction_shadow_evaluations"].create_index' not in source
    assert 'db["prediction_sentinel_routes"].create_index' not in source
    assert 'db["users"].create_index("user_id", unique=True)' in source


def test_operator_cli_routes_shadow_eval_without_touching_doctor():
    source = Path("scripts/planealerts").read_text(encoding="utf-8")
    assert 'sys.argv[1] == "shadow-eval"' in source
    assert "app.shadow_report_v52" in source
