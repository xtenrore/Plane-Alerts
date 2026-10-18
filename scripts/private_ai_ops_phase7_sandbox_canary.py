"""One-shot, credential-free, pinned source sandbox canary on an isolated runner."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from app.private_ops.dr_export import export, restore_empty
from app.private_ops.store import Store
from app.private_ops.tool_gateway import Gateway, TEST_TARGETS, _inside, redact

DIAGNOSTIC_STREAM_BYTES = 3072
DIAGNOSTIC_MAX_BYTES = 8192


def _bounded_excerpt(value: object) -> str:
    if not isinstance(value, str):
        return ''
    data = value.encode('utf-8', 'replace')
    if len(data) <= DIAGNOSTIC_STREAM_BYTES:
        return data.decode('utf-8', 'replace')
    half = DIAGNOSTIC_STREAM_BYTES // 2
    return (data[:half].decode('utf-8', 'replace')
            + '\n...[bounded diagnostic middle omitted]...\n'
            + data[-half:].decode('utf-8', 'replace'))


def _redacted_excerpt(value: object) -> str:
    """Bound and redact a stream before JSON encoding so redaction cannot corrupt JSON."""
    return redact(_bounded_excerpt(value))[0]


def safe_operation_diagnostic(record: dict[str, object]) -> str:
    """Render only bounded, redacted fields already present in a durable tool result."""
    try:
        result = json.loads(str(record.get('result_json') or '{}'))
    except (TypeError, ValueError, json.JSONDecodeError):
        result = {}
    try:
        arguments = json.loads(str(record.get('arguments_json') or '{}'))
    except (TypeError, ValueError, json.JSONDecodeError):
        arguments = {}

    target = result.get('target')
    if not target and record.get('tool') == 'run_replay':
        target = TEST_TARGETS.get(arguments.get('case'))
    elif not target and record.get('tool') == 'run_test':
        target = TEST_TARGETS.get(arguments.get('target'), arguments.get('target'))

    payload = {
        'operation_id': record.get('operation_id'),
        'operation_type': record.get('tool'),
        'status': record.get('status'),
        'result_class': record.get('result_class') or result.get('class'),
        'exit_code': result.get('exit_code'),
        'approved_target': target,
        'source_commit': record.get('source_commit'),
        'timeout_status': record.get('status') == 'TIMED_OUT' or result.get('class') == 'TIMEOUT',
        'output_truncated': bool(result.get('truncated')) or result.get('class') == 'OUTPUT_TRUNCATED',
        'result_hash': record.get('output_hash'),
        'stdout_tail': _redacted_excerpt(result.get('stdout')),
        'stderr_tail': _redacted_excerpt(result.get('stderr')),
    }
    rendered = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    data = rendered.encode('utf-8')
    if len(data) > DIAGNOSTIC_MAX_BYTES:
        rendered = json.dumps({
            'operation_id': record.get('operation_id'),
            'operation_type': record.get('tool'),
            'status': record.get('status'),
            'result_class': record.get('result_class') or result.get('class'),
            'approved_target': target,
            'source_commit': record.get('source_commit'),
            'timeout_status': record.get('status') == 'TIMED_OUT' or result.get('class') == 'TIMEOUT',
            'output_truncated': True,
            'result_hash': record.get('output_hash'),
            'diagnostic_truncated': True,
        }, sort_keys=True)
    return rendered


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if os.environ.get('AI_OPS_SANDBOX_BACKEND') != 'docker':
        raise RuntimeError('isolated container backend required for real sandbox canary')
    image = os.environ.get('AI_OPS_SANDBOX_IMAGE', '')
    with tempfile.TemporaryDirectory(prefix='private-ops-phase7-') as temp:
        root = Path(temp)
        data = root / 'volume'
        data.mkdir()
        scratch = root / 'scratch'
        scratch.mkdir()
        store = Store(data)
        store.enqueue('investigation:canary')
        gateway = Gateway(store, repo, scratch)

        def run(operation_id: str, tool: str, arguments: dict[str, object]) -> dict[str, object]:
            gateway.request(operation_id, 'investigation:canary', tool, arguments, commit)
            record = gateway.execute(operation_id, 'phase7-canary-worker')
            if record['status'] != 'SUCCEEDED':
                print('phase7_operation_failure ' + safe_operation_diagnostic(record), flush=True)
                raise RuntimeError(f'canary operation failed: {operation_id}: {record["result_class"]}')
            return json.loads(record['result_json'])

        def rejected(tool: str, arguments: dict[str, object]) -> None:
            try:
                gateway.request('rejected:' + tool, 'investigation:canary', tool, arguments, commit)
            except ValueError:
                return
            raise AssertionError(f'prohibited operation accepted: {tool}')

        # Structured-gateway negative security surface. No rejected request is queued.
        rejected('read_source', {'path': '../app/private_ops/store.py'})
        rejected('read_source', {'path': '/etc/passwd'})
        rejected('run_test', {'target': 'private_store; id', 'candidate': None})
        rejected('candidate_test', {'path': 'tests/secret.py', 'content': 'token=sk-abcdefghijklmnop'})
        for prohibited in (
            'raw_shell', 'bash', 'delete_file', 'environment_dump', 'read_secret',
            'network_request', 'git_push', 'git_merge', 'git_force_push',
            'railway_mutate', 'github_mutate', 'deploy', 'send_telegram',
        ):
            rejected(prohibited, {})

        # Symlink/path escape containment simulation without touching the repository.
        path_probe = root / 'path-probe'
        (path_probe / 'app').mkdir(parents=True)
        outside = root / 'outside-secret'
        outside.write_text('not-readable-through-approved-path')
        (path_probe / 'app' / 'escape').symlink_to(outside)
        try:
            _inside(path_probe, 'app/escape')
        except ValueError:
            pass
        else:
            raise AssertionError('symlink escape accepted')

        source = run('op:source', 'read_source', {'path': 'app/private_ops/store.py'})
        assert 'SCHEMA_VERSION = 7' in source['summary']
        matches = run('op:search', 'search_source', {'term': 'SCHEMA_VERSION'})
        assert matches['matches']
        pin = run('op:commit', 'inspect_commit', {})
        assert commit in pin['summary']
        replay = run('op:replay', 'run_replay', {'case': 'error_museum'})
        assert replay['class'] == 'PASS' and replay['exit_code'] == 0
        assert replay['target'] == 'tests/test_error_museum_v47.py'

        candidate = run('op:candidate-test', 'candidate_test', {
            'path': 'tests/test_phase7_candidate.py',
            'content': 'def test_isolated_candidate():\n    assert 2 + 2 == 4\n'})
        assert candidate['class'] == 'CANDIDATE_ONLY'
        patched = run('op:candidate-patch', 'candidate_patch', {
            'path': 'app/private_ops/__init__.py',
            'content': (repo / 'app/private_ops/__init__.py').read_text() + '\n# Sandbox only canary edit.\n',
            'candidate': 'op:candidate-test'})
        assert patched['class'] == 'CANDIDATE_ONLY'
        diff = run('op:diff', 'candidate_diff', {'candidate': 'op:candidate-patch'})
        assert 'Sandbox only canary edit' in diff['diff']
        targeted = run('op:targeted', 'run_test', {'target': 'candidate_regression', 'candidate': 'op:candidate-patch'})
        assert targeted['class'] == 'PASS' and targeted['exit_code'] == 0

        # Real bounded-output failure through the controlled gateway.
        gateway.request('op:output', 'investigation:canary', 'run_test',
                        {'target': 'phase7_output', 'candidate': None}, commit)
        output_record = gateway.execute('op:output', 'phase7-canary-worker')
        output_result = json.loads(output_record['result_json'])
        assert output_record['status'] == 'FAILED'
        assert output_result['class'] == 'FAIL'
        assert output_result['truncated'] is True
        assert len(output_result['stdout'].encode()) <= 8192
        assert len(output_result['stderr'].encode()) <= 8192
        assert output_record['output_hash']

        # Real timeout through the controlled gateway, followed by orphan check.
        gateway.request('op:timeout', 'investigation:canary', 'run_test',
                        {'target': 'phase7_timeout', 'candidate': None}, commit)
        timeout_record = gateway.execute('op:timeout', 'phase7-canary-worker')
        assert timeout_record['status'] == 'TIMED_OUT'
        assert timeout_record['result_class'] == 'TIMEOUT'
        running = subprocess.check_output(
            ['docker', 'ps', '-q', '--filter', 'ancestor=' + image], text=True).strip()
        assert not running, 'timed-out sandbox container still running'

        assert 'Sandbox only canary edit' not in (repo / 'app/private_ops/__init__.py').read_text()
        assert subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True).strip() == ''
        assert not list(scratch.iterdir())
        assert store.db.execute('SELECT count(*) FROM ai_ops_tool_operations').fetchone()[0] == 10
        assert store.db.execute('SELECT count(*) FROM ai_ops_tool_operations WHERE source_commit!=?', (commit,)).fetchone()[0] == 0

        data_bytes, manifest = export(store, source_commit=commit)
        recovered_dir = root / 'recovered'
        recovered_dir.mkdir()
        recovered = restore_empty(recovered_dir, data_bytes, manifest)
        try:
            assert recovered.db.execute('SELECT count(*) FROM ai_ops_tool_operations').fetchone()[0] == 10
            assert recovered.db.execute('SELECT count(*) FROM ai_ops_tool_operations WHERE status="SUCCEEDED"').fetchone()[0] == 8
            assert recovered.db.execute('SELECT count(*) FROM ai_ops_tool_operations WHERE status="FAILED"').fetchone()[0] == 1
            assert recovered.db.execute('SELECT count(*) FROM ai_ops_tool_operations WHERE status="TIMED_OUT"').fetchone()[0] == 1
            assert recovered.claim_tool('op:replay', 'recovery') is None
        finally:
            recovered.close()

        replay_hash = store.db.execute('SELECT output_hash FROM ai_ops_tool_operations WHERE operation_id="op:replay"').fetchone()[0]
        output_hash = output_record['output_hash']
        store.close()
        reopened = Store(data)
        try:
            assert reopened.db.execute('SELECT output_hash FROM ai_ops_tool_operations WHERE operation_id="op:replay"').fetchone()[0] == replay_hash
            assert Gateway(reopened, repo, scratch).execute('op:replay', 'recovery')['output_hash'] == replay_hash
            assert Gateway(reopened, repo, scratch).execute('op:output', 'recovery')['output_hash'] == output_hash
            assert Gateway(reopened, repo, scratch).execute('op:timeout', 'recovery')['status'] == 'TIMED_OUT'
            assert reopened.db.execute('SELECT count(*) FROM ai_ops_findings WHERE status="RESOLVED"').fetchone()[0] == 0
        finally:
            reopened.close()

        print(json.dumps({
            'phase7_sandbox_canary': 'PASS', 'commit': commit, 'operations': 10,
            'replay_target': replay['target'], 'targeted_test': targeted['target'],
            'dr_sha256': hashlib.sha256(data_bytes).hexdigest(),
            'output_bounded': True, 'timeout_cleanup': True, 'security_rejections': 17,
            'no_push_merge_deploy': True,
        }, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
