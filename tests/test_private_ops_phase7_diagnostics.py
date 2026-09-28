"""Regression coverage for safe Phase 7 failed-operation diagnostics."""
import json

from scripts.private_ai_ops_phase7_sandbox_canary import (
    DIAGNOSTIC_MAX_BYTES,
    safe_operation_diagnostic,
)


def test_failed_replay_diagnostic_is_debuggable_redacted_and_bounded():
    result = {
        'class': 'FAIL',
        'exit_code': 1,
        'target': 'tests/test_error_museum_v47.py',
        'stdout': ('x' * 5000) + '\nREPLAY_ASSERTION_MARKER\nAuthorization: Bearer definitely-secret-value',
        'stderr': 'pytest stderr tail',
        'truncated': True,
    }
    record = {
        'operation_id': 'op:replay',
        'tool': 'run_replay',
        'status': 'FAILED',
        'result_class': 'FAIL',
        'source_commit': 'a' * 40,
        'arguments_json': json.dumps({'case': 'error_museum'}),
        'result_json': json.dumps(result),
        'output_hash': 'b' * 64,
    }

    rendered = safe_operation_diagnostic(record)
    payload = json.loads(rendered)

    assert payload['operation_id'] == 'op:replay'
    assert payload['operation_type'] == 'run_replay'
    assert payload['status'] == 'FAILED'
    assert payload['exit_code'] == 1
    assert payload['approved_target'] == 'tests/test_error_museum_v47.py'
    assert payload['source_commit'] == 'a' * 40
    assert payload['timeout_status'] is False
    assert payload['output_truncated'] is True
    assert payload['result_hash'] == 'b' * 64
    assert 'REPLAY_ASSERTION_MARKER' in payload['stdout_tail']
    assert 'pytest stderr tail' in payload['stderr_tail']
    assert 'definitely-secret-value' not in rendered
    assert '[REDACTED]' in rendered
    assert len(rendered.encode('utf-8')) <= DIAGNOSTIC_MAX_BYTES
