"""Structured gateway and durable Phase 7 operations."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app.private_ops.store import SCHEMA_VERSION, Store
from app.private_ops.tool_gateway import Gateway, _arguments, _inside, redact
from app.private_ops.dr_export import export, restore_empty


@pytest.fixture
def gateway(tmp_path):
    repo = tmp_path / 'source'
    repo.mkdir()
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True)
    (repo / 'app').mkdir()
    (repo / 'app' / 'module.py').write_text('def answer():\n    return 42\n')
    (repo / 'tests').mkdir()
    (repo / 'tests' / 'test_example.py').write_text('def test_answer():\n    assert True\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.email=test@example.invalid',
                    '-c', 'user.name=Test', 'commit', '-qm', 'fixture'], check=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    data = tmp_path / 'data'
    data.mkdir()
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    store = Store(data)
    store.enqueue('investigation:one')
    yield Gateway(store, repo, scratch), commit, repo, scratch
    store.close()


def test_pinned_source_and_immutable_result(gateway):
    gw, commit, repo, scratch = gateway
    gw.request('op-source', 'investigation:one', 'read_source', {'path': 'app/module.py'}, commit)
    result = gw.execute('op-source', 'worker')
    assert result['status'] == 'SUCCEEDED'
    assert 'return 42' in json.loads(result['result_json'])['summary']
    assert gw.execute('op-source', 'other')['output_hash'] == result['output_hash']
    assert list(scratch.iterdir()) == []
    assert (repo / 'app/module.py').read_text().endswith('return 42\n')


def test_search_and_commit(gateway):
    gw, commit, _, _ = gateway
    for tool, args in [('search_source', {'term': 'answer'}), ('inspect_commit', {})]:
        operation_id = 'op-' + tool
        gw.request(operation_id, 'investigation:one', tool, args, commit)
        assert gw.execute(operation_id, 'worker')['status'] == 'SUCCEEDED'


@pytest.mark.parametrize('tool,args', [
    ('shell', {'command': 'env'}), ('bash', {'command': 'rm -rf /'}),
    ('read_source', {'path': '../.env'}), ('read_source', {'path': 'app/../.env'}),
    ('read_source', {'path': '.git/config'}), ('read_source', {'path': '/etc/passwd'}),
    ('run_test', {'target': 'tests/../../etc/passwd', 'candidate': None}),
    ('run_test', {'target': 'private_store; env', 'candidate': None}),
    ('run_replay', {'case': 'unapproved'}),
    ('candidate_patch', {'path': 'app/a.py', 'content': 'API_KEY=sk-123456789123456789', 'candidate': None}),
])
def test_prohibited_operations_rejected_before_persistence(gateway, tool, args):
    gw, commit, _, _ = gateway
    with pytest.raises(ValueError):
        gw.request('reject', 'investigation:one', tool, args, commit)
    assert gw.store.db.execute('SELECT count(*) FROM ai_ops_tool_operations').fetchone()[0] == 0


def test_candidates_remain_in_isolated_snapshot(gateway):
    gw, commit, repo, scratch = gateway
    gw.request('test-candidate', 'investigation:one', 'candidate_test',
               {'path': 'tests/test_regression.py', 'content': 'def test_candidate():\n    assert True\n'}, commit)
    first = gw.execute('test-candidate', 'worker')
    assert first['status'] == 'SUCCEEDED' and first['artifact_hash']
    assert not (repo / 'tests/test_regression.py').exists()
    gw.request('patch-candidate', 'investigation:one', 'candidate_patch',
               {'path': 'app/module.py', 'content': 'def answer():\n    return 43\n', 'candidate': 'test-candidate'}, commit)
    second = gw.execute('patch-candidate', 'worker')
    assert second['status'] == 'SUCCEEDED'
    gw.request('diff', 'investigation:one', 'candidate_diff', {'candidate': 'patch-candidate'}, commit)
    diff = json.loads(gw.execute('diff', 'worker')['result_json'])['diff']
    assert '-    return 42' in diff and '+    return 43' in diff
    assert (repo / 'app/module.py').read_text().endswith('return 42\n')
    assert not list(scratch.iterdir())
    assert gw.store.db.execute('SELECT count(*) FROM ai_ops_findings WHERE status="RESOLVED"').fetchone()[0] == 0


def test_expired_lease_never_reruns_uncertain_work(gateway):
    gw, commit, _, _ = gateway
    gw.request('uncertain', 'investigation:one', 'read_source', {'path': 'app/module.py'}, commit)
    assert gw.store.claim_tool('uncertain', 'worker')
    with gw.store.transaction() as db:
        db.execute('UPDATE ai_ops_tool_operations SET lease_until=? WHERE operation_id=?', (time.time()-1, 'uncertain'))
    assert gw.store.claim_tool('uncertain', 'other') is None
    assert gw.execute('uncertain', 'third')['result_class'] == 'INTERRUPTED'


def test_duplicate_worker_and_persistent_result(gateway):
    gw, commit, _, _ = gateway
    gw.request('once', 'investigation:one', 'repository_status', {}, commit)
    assert gw.store.claim_tool('once', 'a')
    with pytest.raises(PermissionError):
        gw.store.claim_tool('once', 'b')
    finished = gw.store.finish_tool('once', 'a', 'SUCCEEDED', {'class': 'OK'})
    reopened = Store(gw.store.root)
    try:
        assert reopened.health()['schema'] == SCHEMA_VERSION == 7
        assert reopened.claim_tool('once', 'b') is None
        assert reopened.db.execute('SELECT output_hash FROM ai_ops_tool_operations WHERE operation_id="once"').fetchone()[0] == finished['output_hash']
    finally:
        reopened.close()


def test_redaction_and_fail_closed_runner(gateway):
    gw, commit, _, _ = gateway
    assert redact('Authorization: Bearer definitely-secret')[0] == '[REDACTED]'
    assert redact('AIza12345678901234567890')[0] == '[REDACTED]'
    gw.request('sandbox', 'investigation:one', 'run_test', {'target': 'private_store', 'candidate': None}, commit)
    result = gw.execute('sandbox', 'worker')
    assert result['status'] == 'FAILED'  # fixture intentionally has no approved test file
    assert result['result_class'] in ('RuntimeError', 'FAIL')


def test_tool_provenance_and_candidate_survive_private_dr(gateway, tmp_path):
    gw, commit, _, _ = gateway
    gw.request('candidate-dr', 'investigation:one', 'candidate_test',
               {'path': 'tests/test_candidate.py', 'content': 'def test_case():\n    assert 42 == 42\n'}, commit)
    finished = gw.execute('candidate-dr', 'worker')
    data, manifest = export(gw.store, source_commit=commit)
    assert b'Authorization' not in data and manifest['tool_operations'] == 1
    target = tmp_path / 'restore'
    target.mkdir()
    restored = restore_empty(target, data, manifest)
    try:
        operation = restored.db.execute('SELECT * FROM ai_ops_tool_operations WHERE operation_id="candidate-dr"').fetchone()
        assert operation['source_commit'] == commit
        assert operation['artifact_hash'] == finished['artifact_hash']
        assert json.loads(operation['arguments_json'])['content'].endswith('42 == 42\n')
        assert restored.claim_tool('candidate-dr', 'recovery') is None
    finally:
        restored.close()


def test_schema_six_upgrade_preserves_reviews_feedback_and_jobs(tmp_path):
    root = tmp_path / 'volume'
    root.mkdir()
    store = Store(root)
    store.enqueue('existing-job')
    store.db.execute('DROP TABLE ai_ops_tool_operations')
    store.db.execute('PRAGMA user_version=6')
    store.close()
    upgraded = Store(root)
    try:
        assert upgraded.health()['schema'] == 7
        assert upgraded.db.execute('SELECT id FROM ai_ops_jobs').fetchone()[0] == 'existing-job'
        assert {'ai_ops_reviews', 'ai_ops_feedback', 'ai_ops_provider_health',
                'ai_ops_tool_operations'} <= {r[0] for r in upgraded.db.execute('SELECT name FROM sqlite_master')}
    finally:
        upgraded.close()
