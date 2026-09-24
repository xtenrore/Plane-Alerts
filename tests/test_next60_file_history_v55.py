from __future__ import annotations

import inspect

from app.bot import next60


def test_history_clock_handles_midnight_wrap():
    values = [23 * 3600 + 55 * 60, 5 * 60, 10 * 60]
    median = next60._median_time_of_day(values)[0]
    assert median < 30 * 60 or median > 23 * 3600


def test_history_confidence_never_promotes_30_to_60_minute_shadow():
    assert next60._history_confidence(5, 120.0, 12 * 60) in {"High", "Medium"}
    assert next60._history_confidence(20, 10.0, 31 * 60) == "Low"
    assert next60._history_confidence(100, 1.0, 59 * 60) == "Low"


def test_next60_no_longer_reads_prediction_lab_mongo():
    source = inspect.getsource(next60)
    assert 'get_db()["prediction_lab_audit"]' not in source
    assert '"prediction_lab_audit"' not in source
    assert "flight_route_samples" in source
    assert "30–60 min history entries remain shadow forecasts" in source


def test_next60_history_evidence_marks_missing_future_coverage_inconclusive():
    source = inspect.getsource(next60)
    assert '"missing_coverage_policy": "inconclusive"' in source
    assert '"outcome_resolution": "inconclusive_until_observed"' in source
    assert '"shadow_only": True' in source
