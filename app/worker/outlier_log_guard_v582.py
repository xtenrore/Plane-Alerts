"""Bound repetitive ADS-B outlier diagnostics without changing rejection behavior."""
from __future__ import annotations

import logging
import threading
import time

_INTERVAL_S = 60.0
_lock = threading.Lock()
_last_zero_jump_log = 0.0
_suppressed_zero_jump = 0
_installed = False


class _ZeroJumpOutlierFilter(logging.Filter):
    """Allow one zero-jump warning per interval and summarize suppressed repeats."""

    def filter(self, record: logging.LogRecord) -> bool:
        global _last_zero_jump_log, _suppressed_zero_jump
        try:
            message = record.getMessage()
        except Exception:
            return True
        if "adsb_outlier_rejected" not in message or "jump_km=0.00" not in message:
            return True

        now = time.monotonic()
        with _lock:
            if _last_zero_jump_log and now - _last_zero_jump_log < _INTERVAL_S:
                _suppressed_zero_jump += 1
                return False
            suppressed = _suppressed_zero_jump
            _suppressed_zero_jump = 0
            _last_zero_jump_log = now

        if suppressed:
            record.msg = "%s suppressed_zero_jump_since_last=%d"
            record.args = (message, suppressed)
        return True


def install_outlier_log_guard_v582() -> None:
    """Install the filter on the exact logger emitting shared-poller outlier warnings."""
    global _installed
    if _installed:
        return
    logging.getLogger("app.worker.v35").addFilter(_ZeroJumpOutlierFilter())
    _installed = True
