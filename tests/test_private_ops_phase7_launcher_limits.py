"""Regression coverage for Phase 7 host-launcher resource limits."""
import resource

from app.private_ops.tool_gateway import _launcher_limits


def _captured_limits(monkeypatch, backend: str):
    calls = []
    monkeypatch.setattr(resource, 'setrlimit', lambda kind, value: calls.append((kind, value)))
    _launcher_limits(90, backend)()
    return calls


def test_docker_launcher_does_not_receive_container_address_space_limit(monkeypatch):
    calls = _captured_limits(monkeypatch, 'docker')
    kinds = {kind for kind, _ in calls}
    assert resource.RLIMIT_AS not in kinds
    assert resource.RLIMIT_CPU in kinds
    assert resource.RLIMIT_FSIZE in kinds


def test_bwrap_sandbox_process_keeps_address_space_limit(monkeypatch):
    calls = _captured_limits(monkeypatch, 'bwrap')
    limits = dict(calls)
    assert limits[resource.RLIMIT_AS] == (768 * 1024 * 1024, 768 * 1024 * 1024)
    assert limits[resource.RLIMIT_CPU] == (70, 70)
    assert limits[resource.RLIMIT_FSIZE] == (64 * 1024, 64 * 1024)
