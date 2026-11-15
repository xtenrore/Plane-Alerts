"""Fail-closed structured engineering tools. No model-supplied shell or host paths.

Execution requires an approved isolated backend. When the host does not permit
one, execution fails safely; there is deliberately no shell fallback. The
backend checks out an immutable Git archive into disposable scratch space.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import resource
import re
import signal
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .store import Store

COMMIT = re.compile(r"[0-9a-f]{40}\Z")
PATH = re.compile(r"[A-Za-z0-9_.\-/]{1,160}\Z")
SAFE_SOURCE = ("app/", "tests/", "scripts/", "docs/private-ai-ops/")
TEST_TARGETS = {
    "private_store": "tests/test_private_ops_store.py",
    "private_review": "tests/test_private_ops_phase6.py",
    "error_museum": "tests/test_error_museum_v47.py",
    "prediction_lab": "tests/test_prediction_lab_pipeline_v55.py",
    "phase7_output": "scripts/private_ai_ops_phase7_output_fixture.py",
    "phase7_timeout": "scripts/private_ai_ops_phase7_timeout_fixture.py",
}
TEST_TIMEOUTS = {"phase7_timeout": 2}
MAX_OUTPUT = 8192
MAX_ARTIFACT = 8192
TIMEOUT = 90
SECRET = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?[^\s]+|bearer\s+[^\s]+|"
    r"(?:gh[pousr]_|sk-|gsk_|AIza)[A-Za-z0-9_-]{12,}|"
    r"(?:mongodb(?:\+srv)?://)[^\s]+|(?:api[_-]?key|password|token|secret)\s*[:=]\s*[^\s]+|"
    r"\b(?:latitude|longitude|lat|lon)\s*[:=]\s*-?\d+(?:\.\d+)?)"
)


def redact(value: str) -> tuple[str, bool]:
    value, count = SECRET.subn('[REDACTED]', value)
    return value, bool(count)


def safe_path(path: str, *, editable: bool = False) -> str:
    if not isinstance(path, str) or not PATH.fullmatch(path) or path.startswith('/') or '\\' in path:
        raise ValueError('invalid source path')
    if any(part in ('.', '..', '.git', '__pycache__') for part in Path(path).parts):
        raise ValueError('source path escape')
    prefixes = ('tests/',) if editable else SAFE_SOURCE
    if not path.startswith(prefixes):
        raise ValueError('path outside approved source tree')
    if editable and not path.endswith('.py'):
        raise ValueError('candidate tests must be Python files')
    return path


def _arguments(tool: str, arguments: dict[str, object]) -> dict[str, object]:
    if not isinstance(arguments, dict):
        raise ValueError('structured arguments required')
    schema = {
        'repository_status': set(), 'list_files': set(), 'inspect_commit': set(),
        'read_source': {'path'}, 'search_source': {'term'}, 'run_test': {'target', 'candidate'},
        'run_replay': {'case'}, 'candidate_test': {'path', 'content'},
        'candidate_patch': {'path', 'content', 'candidate'}, 'candidate_diff': {'candidate'},
    }
    if tool not in schema or set(arguments) != schema[tool]:
        raise ValueError('tool or arguments are not allowlisted')
    if tool in ('read_source', 'candidate_test', 'candidate_patch'):
        safe_path(arguments['path'], editable=tool == 'candidate_test')
        if tool == 'candidate_patch' and not str(arguments['path']).startswith('app/'):
            raise ValueError('candidate source patch outside app')
    if tool == 'search_source':
        term = arguments['term']
        if not isinstance(term, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{2,64}', term):
            raise ValueError('invalid literal source search')
    if tool == 'run_test' and arguments['target'] not in (*TEST_TARGETS, 'candidate_regression'):
        raise ValueError('unknown approved test target')
    if tool == 'run_test' and arguments['target'] == 'candidate_regression' and not arguments['candidate']:
        raise ValueError('candidate regression target requires a candidate')
    if tool in ('run_test', 'candidate_patch', 'candidate_diff'):
        value = arguments['candidate']
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,128}', value)):
            raise ValueError('invalid candidate operation id')
    if tool == 'run_replay' and arguments['case'] not in ('error_museum', 'prediction_lab'):
        raise ValueError('unknown approved replay case')
    if tool in ('candidate_test', 'candidate_patch'):
        content = arguments['content']
        if not isinstance(content, str) or not content or len(content.encode()) > MAX_ARTIFACT:
            raise ValueError('candidate artifact exceeds bounds')
        if SECRET.search(content):
            raise ValueError('candidate contains secret-like content')
    return arguments


def _checked_source(repository: Path, commit: str) -> None:
    if not COMMIT.fullmatch(commit):
        raise ValueError('exact commit required')
    result = subprocess.run(['git', '-C', str(repository), 'cat-file', '-t', commit],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False)
    if result.returncode or result.stdout.strip() != b'commit':
        raise ValueError('unknown source commit')


def _snapshot(repository: Path, commit: str, destination: Path) -> None:
    result = subprocess.run(['git', '-C', str(repository), 'archive', '--format=tar', commit],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=True)
    if len(result.stdout) > 32_000_000:
        raise ValueError('source snapshot exceeds bounds')
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        members = archive.getmembers()
        if len(members) > 3000:
            raise ValueError('source snapshot has too many files')
        for member in members:
            name = member.name.rstrip('/')
            if (not name or name.startswith('/') or any(p in ('.', '..') for p in name.split('/'))
                    or not (member.isfile() or member.isdir()) or member.size > 1_000_000):
                raise ValueError('unsafe source archive member')
        archive.extractall(destination, filter='data')


def _inside(root: Path, path: str) -> Path:
    target = root / safe_path(path)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError('source path escapes snapshot')
    if target.is_symlink():
        raise ValueError('symlink source path denied')
    return target


def _launcher_limits(timeout: int, backend: str):
    """Return host-launcher limits without applying sandbox memory to Docker itself."""
    def limits() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (min(timeout, 70), min(timeout, 70)))
        # For bwrap the launcher becomes the sandboxed Python process, so an
        # address-space limit belongs here. For Docker the launcher is the
        # trusted Go client; the untrusted container is instead bounded by
        # Docker's --memory/--cpus/--pids-limit controls below.
        if backend != 'docker':
            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024, 64 * 1024))
    return limits


def _cleanup_docker_container(docker: str, cid_file: Path, env: dict[str, str]) -> None:
    """Best-effort deterministic cleanup of a timed-out Docker sandbox."""
    try:
        container_id = cid_file.read_text().strip()
    except OSError:
        return
    if re.fullmatch(r'[0-9a-f]{12,64}', container_id):
        subprocess.run([docker, 'rm', '-f', container_id], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=10, check=False)


def _sandbox_command(root: Path, target: str, timeout: int) -> tuple[str, str, int, bool]:
    """Run fixed Python pytest target, with no network and no host writable mounts."""
    backend = os.environ.get('AI_OPS_SANDBOX_BACKEND', 'bwrap')
    docker = None
    cid_file = None
    if backend == 'docker':
        image = os.environ.get('AI_OPS_SANDBOX_IMAGE', '')
        if not re.fullmatch(r'plane-alerts-investigator:[0-9a-f]{40}', image):
            raise RuntimeError('exact approved sandbox image required')
        docker = shutil.which('docker')
        if not docker:
            raise RuntimeError('isolated Docker runner unavailable')
        cid_file = root.parent / (root.name + '.cid')
        command = [docker, 'run', '--rm', '--pull', 'never', '--cidfile', str(cid_file),
                   '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--pids-limit', '64',
                   '--memory', '768m', '--cpus', '1', '--user', '65534:65534',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=32m',
                   '--mount', 'type=bind,src=' + str(root) + ',dst=/work,readonly',
                   '--workdir', '/work', '--env', 'HOME=/tmp', '--env', 'PYTHONPATH=/work',
                   image, 'python', '-m', 'pytest', '-q', target]
    elif backend == 'bwrap':
        bwrap = shutil.which('bwrap')
        if not bwrap:
            raise RuntimeError('isolated sandbox unavailable')
        python = Path('/usr/local/bin/python')
        if not python.is_file():
            raise RuntimeError('approved sandbox Python runtime unavailable')
        command = [bwrap, '--unshare-all', '--die-with-parent', '--new-session',
                   '--ro-bind', '/usr', '/usr', '--ro-bind', '/bin', '/bin',
                   '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                   '--ro-bind', str(root), '/work', '--chdir', '/work',
                   '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
                   '--setenv', 'HOME', '/tmp', '--setenv', 'PYTHONPATH', '/work',
                   '--', '/usr/local/bin/python', '-m', 'pytest', '-q', target]
        for link in ('/lib', '/lib64'):
            if Path(link).exists():
                command[command.index('--proc'):command.index('--proc')] = ['--ro-bind', link, link]
    else:
        raise RuntimeError('unknown sandbox backend')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(command, env=env, stdout=stdout_file, stderr=stderr_file,
                                       start_new_session=True, preexec_fn=_launcher_limits(timeout, backend))
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                if docker and cid_file:
                    _cleanup_docker_container(docker, cid_file, env)
                raise TimeoutError('sandbox time limit exceeded') from exc
            stdout_file.seek(0)
            stderr_file.seek(0)
            out = stdout_file.read(MAX_OUTPUT + 1)
            err = stderr_file.read(MAX_OUTPUT + 1)
            return (out[:MAX_OUTPUT].decode('utf-8', 'replace'), err[:MAX_OUTPUT].decode('utf-8', 'replace'),
                    process.returncode, len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT)
    finally:
        if cid_file:
            cid_file.unlink(missing_ok=True)


class Gateway:
    def __init__(self, store: Store, repository: str | Path, scratch: str | Path):
        self.store = store
        self.repository = Path(repository).resolve()
        self.scratch = Path(scratch).resolve()
        if not self.scratch.is_dir() or self.scratch.is_symlink():
            raise ValueError('existing isolated scratch root required')

    def request(self, operation_id: str, job_id: str, tool: str, arguments: dict[str, object],
                source_commit: str, *, finding_id: str | None = None) -> dict[str, object]:
        _arguments(tool, arguments)
        _checked_source(self.repository, source_commit)
        return self.store.queue_tool(operation_id, job_id, tool, arguments, source_commit, finding_id=finding_id)

    def execute(self, operation_id: str, worker: str) -> dict[str, object]:
        row = self.store.claim_tool(operation_id, worker)
        if row is None:
            return dict(self.store.db.execute('SELECT * FROM ai_ops_tool_operations WHERE operation_id=?',
                                              (operation_id,)).fetchone())
        root = Path(tempfile.mkdtemp(prefix='investigate-', dir=self.scratch))
        try:
            _snapshot(self.repository, row['source_commit'], root)
            if os.environ.get('AI_OPS_SANDBOX_BACKEND') == 'docker':
                root.chmod(0o755)
                for member in root.rglob('*'):
                    member.chmod(0o755 if member.is_dir() else 0o644)
            tool = row['tool']
            args = json.loads(row['arguments_json'])
            candidate = args.get('candidate')
            candidate_test_path = None
            direct_candidate_path = None
            ancestry = set()
            while candidate:
                if candidate in ancestry or len(ancestry) >= 3:
                    raise ValueError('candidate chain exceeds bounds')
                ancestry.add(candidate)
                previous = self.store.db.execute('SELECT * FROM ai_ops_tool_operations WHERE operation_id=?',
                                                 (candidate,)).fetchone()
                if (not previous or previous['job_id'] != row['job_id']
                        or previous['source_commit'] != row['source_commit']
                        or previous['status'] != 'SUCCEEDED'
                        or previous['tool'] not in ('candidate_test', 'candidate_patch')):
                    raise ValueError('candidate provenance mismatch')
                previous_args = json.loads(previous['arguments_json'])
                if direct_candidate_path is None:
                    direct_candidate_path = previous_args['path']
                previous_result = json.loads(previous['result_json'])
                previous_path = _inside(root, previous_args['path'])
                previous_path.parent.mkdir(parents=True, exist_ok=True)
                previous_path.write_text(previous_result['artifact'])
                if hashlib.sha256(previous_path.read_bytes()).hexdigest() != previous['artifact_hash']:
                    raise ValueError('candidate artifact checksum mismatch')
                if previous['tool'] == 'candidate_test':
                    candidate_test_path = previous_args['path']
                candidate = previous_args.get('candidate')
            artifact_hash = None
            result: dict[str, object] = {'class': 'OK', 'source_commit': row['source_commit']}
            if tool == 'repository_status':
                result['summary'] = 'Immutable clean source snapshot at pinned commit'
            elif tool == 'list_files':
                result['files'] = sorted(str(p.relative_to(root)) for p in root.rglob('*')
                                         if p.is_file() and str(p.relative_to(root)).startswith(SAFE_SOURCE))[:200]
            elif tool == 'inspect_commit':
                commit = subprocess.run(['git', '-C', str(self.repository), 'show', '-s', '--format=%H%n%s',
                                         row['source_commit']], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        check=True, timeout=5).stdout.decode('utf-8', 'replace')
                result['summary'] = commit[:256]
            elif tool == 'read_source':
                result['summary'] = _inside(root, args['path']).read_bytes()[:MAX_OUTPUT].decode('utf-8', 'replace')
            elif tool == 'search_source':
                matches = []
                for file in root.rglob('*.py'):
                    if not str(file.relative_to(root)).startswith(SAFE_SOURCE):
                        continue
                    for number, line in enumerate(file.read_text(errors='replace').splitlines(), 1):
                        if args['term'] in line:
                            matches.append(f'{file.relative_to(root)}:{number}:{line[:160]}')
                            if len(matches) >= 32:
                                break
                    if len(matches) >= 32:
                        break
                result['matches'] = matches
            elif tool in ('candidate_test', 'candidate_patch'):
                target = _inside(root, args['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args['content'])
                artifact_hash = hashlib.sha256(args['content'].encode()).hexdigest()
                result['changed_file'] = args['path']
                result['artifact_hash'] = artifact_hash
                result['artifact'] = args['content']
                result['class'] = 'CANDIDATE_ONLY'
            elif tool in ('run_test', 'run_replay'):
                target = (candidate_test_path if args['target'] == 'candidate_regression' else TEST_TARGETS[args['target']]) if tool == 'run_test' else TEST_TARGETS[args['case']]
                if target is None:
                    raise ValueError('candidate regression test missing')
                operation_timeout = TEST_TIMEOUTS.get(args['target'], TIMEOUT) if tool == 'run_test' else TIMEOUT
                out, err, code, truncated = _sandbox_command(root, target, operation_timeout)
                result.update(stdout=out, stderr=err, exit_code=code, truncated=truncated, target=target)
                result['class'] = 'PASS' if code == 0 else 'FAIL'
            elif tool == 'candidate_diff':
                target = _inside(root, direct_candidate_path)
                original = subprocess.run(['git', '-C', str(self.repository), 'show',
                                           row['source_commit'] + ':' + direct_candidate_path],
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          check=False, timeout=5).stdout
                import difflib
                diff = ''.join(difflib.unified_diff(original.decode(errors='replace').splitlines(True),
                               target.read_text().splitlines(True), fromfile='base/' + direct_candidate_path,
                               tofile='candidate/' + direct_candidate_path))
                if len(diff.encode()) > MAX_ARTIFACT:
                    raise ValueError('candidate diff exceeds bounds')
                result['diff'] = diff
                artifact_hash = hashlib.sha256(diff.encode()).hexdigest()
            else:
                raise ValueError('unrecognized operation')
            serialized = json.dumps(result, sort_keys=True)
            redacted, found = redact(serialized)
            result = json.loads(redacted)
            if found:
                result = {'class': 'SECURITY_REDACTED', 'summary': 'Secret-like tool output blocked',
                          'source_commit': row['source_commit']}
            if len(json.dumps(result).encode()) > 15000:
                result = {'class': 'OUTPUT_TRUNCATED', 'summary': 'Bounded result exceeded durable limit',
                          'source_commit': row['source_commit']}
            return self.store.finish_tool(operation_id, worker,
                                          'SUCCEEDED' if result['class'] not in ('FAIL', 'SECURITY_REDACTED') else 'FAILED',
                                          result, artifact_hash=artifact_hash if not found else None)
        except TimeoutError:
            return self.store.finish_tool(operation_id, worker, 'TIMED_OUT', {'class': 'TIMEOUT'})
        except Exception as exc:
            return self.store.finish_tool(operation_id, worker, 'FAILED', {'class': type(exc).__name__})
        finally:
            shutil.rmtree(root)
