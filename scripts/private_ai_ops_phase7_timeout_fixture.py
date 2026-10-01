"""Deliberately hanging fixed Phase 7 sandbox fixture; not pytest-discovered globally."""
import time


def test_phase7_timeout_fixture():
    time.sleep(30)
