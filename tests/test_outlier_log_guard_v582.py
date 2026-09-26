from __future__ import annotations

import logging


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord("app.worker.v35", logging.WARNING, __file__, 1, message, (), None)


def test_zero_jump_outlier_warnings_are_bounded_and_summarized(monkeypatch):
    from app.worker import outlier_log_guard_v582 as guard

    guard._last_zero_jump_log = 0.0
    guard._suppressed_zero_jump = 0
    ticks = iter([100.0, 101.0, 102.0, 161.0])
    monkeypatch.setattr(guard.time, "monotonic", lambda: next(ticks))
    filt = guard._ZeroJumpOutlierFilter()

    first = _record("adsb_outlier_rejected icao=aaaaaa jump_km=0.00 sample_age=6.0")
    second = _record("adsb_outlier_rejected icao=bbbbbb jump_km=0.00 sample_age=7.0")
    third = _record("adsb_outlier_rejected icao=cccccc jump_km=0.00 sample_age=8.0")
    summary = _record("adsb_outlier_rejected icao=dddddd jump_km=0.00 sample_age=9.0")

    assert filt.filter(first) is True
    assert filt.filter(second) is False
    assert filt.filter(third) is False
    assert filt.filter(summary) is True
    assert "suppressed_zero_jump_since_last=2" in summary.getMessage()


def test_nonzero_outlier_warning_is_never_suppressed(monkeypatch):
    from app.worker import outlier_log_guard_v582 as guard

    guard._last_zero_jump_log = 100.0
    guard._suppressed_zero_jump = 99
    monkeypatch.setattr(guard.time, "monotonic", lambda: 101.0)
    filt = guard._ZeroJumpOutlierFilter()
    record = _record("adsb_outlier_rejected icao=aaaaaa jump_km=4.21 sample_age=40.4")

    assert filt.filter(record) is True
    assert record.getMessage().endswith("sample_age=40.4")
    assert guard._suppressed_zero_jump == 99
