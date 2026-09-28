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
from app.private_ops.tool_gateway import Gateway


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if os.environ.get('AI_OPS_SANDBOX_BACKEND') != 'docker':
        raise RuntimeError('isolated container backend required for real sandbox canary')
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
                raise RuntimeError(f'canary operation failed: {operation_id}: {record["result_class"]}')
            return json.loads(record['result_json'])

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
        assert 'Sandbox only canary edit' not in (repo / 'app/private_ops/__init__.py').read_text()
        assert not list(scratch.iterdir())
        data_bytes, manifest = export(store, source_commit=commit)
        recovered_dir = root / 'recovered'
        recovered_dir.mkdir()
        recovered = restore_empty(recovered_dir, data_bytes, manifest)
        try:
            assert recovered.db.execute('SELECT count(*) FROM ai_ops_tool_operations WHERE status="SUCCEEDED"').fetchone()[0] == 8
            assert recovered.claim_tool('op:replay', 'recovery') is None
        finally:
            recovered.close()
        original = store.db.execute('SELECT output_hash FROM ai_ops_tool_operations WHERE operation_id="op:replay"').fetchone()[0]
        store.close()
        reopened = Store(data)
        try:
            assert reopened.db.execute('SELECT output_hash FROM ai_ops_tool_operations WHERE operation_id="op:replay"').fetchone()[0] == original
            assert Gateway(reopened, repo, scratch).execute('op:replay', 'recovery')['output_hash'] == original
            assert reopened.db.execute('SELECT count(*) FROM ai_ops_findings WHERE status="RESOLVED"').fetchone()[0] == 0
        finally:
            reopened.close()
        print(json.dumps({'phase7_sandbox_canary': 'PASS', 'commit': commit,
                          'operations': 8, 'replay_target': replay['target'],
                          'targeted_test': targeted['target'], 'dr_sha256': hashlib.sha256(data_bytes).hexdigest(),
                          'no_push_merge_deploy': True}, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
