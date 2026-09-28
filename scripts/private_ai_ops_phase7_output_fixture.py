"""Deliberately noisy fixed Phase 7 sandbox fixture; not pytest-discovered globally."""


def test_phase7_bounded_output_fixture():
    raise AssertionError('PHASE7_OUTPUT_BOUND:' + ('X' * 20000))
