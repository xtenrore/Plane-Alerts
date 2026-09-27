"""Phase 1 crash, lease and bounded-queue invariants."""
import sqlite3
import threading
import queue
from http.server import HTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from app.private_ops.store import QueueFull, Store
from app.private_ops.service import create_handler
from app.private_ops.phase1_probe import run as run_phase1_probe


def test_restart_preserves_committed_step_and_reclaims_expired_job(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.private_ops.store.time.time", lambda: clock[0])
    first = Store(tmp_path)
    assert first.enqueue("audit:1")
    assert not first.enqueue("audit:1")
    assert first.claim("worker-a", lease_seconds=10) == "audit:1"
    assert first.begin_step("audit:1", "packet:1", "worker-a", {"event": "one"})
    committed_hash = first.complete_step("audit:1", "packet:1", "worker-a", {"valid": True})
    assert first.begin_step("audit:1", "packet:2", "worker-a", {"event": "two"})
    first.close()

    second = Store(tmp_path)
    assert second.claim("worker-b") is None
    clock[0] += 11
    assert second.claim("worker-b") == "audit:1"
    assert not second.begin_step("audit:1", "packet:1", "worker-b", {"event": "one"})
    assert second.complete_step("audit:1", "packet:1", "worker-b", {"valid": True}) == committed_hash
    with pytest.raises(ValueError, match="different input"):
        second.begin_step("audit:1", "packet:1", "worker-b", {"event": "changed"})
    with pytest.raises(RuntimeError, match="incomplete steps"):
        second.finish_job("audit:1", "worker-b")
    assert second.begin_step("audit:1", "packet:2", "worker-b", {"event": "two"})
    second.complete_step("audit:1", "packet:2", "worker-b", {"valid": False})
    second.finish_job("audit:1", "worker-b")
    assert second.claim("worker-c") is None
    assert second.health() == {"schema": 1, "integrity": "ok", "pending": 0}
    second.close()


def test_bounded_queue_and_lease_ownership(tmp_path):
    a = Store(tmp_path, max_pending=1)
    b = Store(tmp_path, max_pending=1)
    assert a.enqueue("first")
    with pytest.raises(QueueFull):
        b.enqueue("second")
    assert b.claim("b") == "first"
    assert a.claim("a") is None
    with pytest.raises(PermissionError):
        a.begin_step("first", "s", "a", {})
    assert not a.heartbeat("first", "a")
    assert b.heartbeat("first", "b")
    with pytest.raises(ValueError, match="size limit"):
        b.complete_step("first", "s", "b", "x" * 70000)
    b.finish_job("first", "b")
    assert a.enqueue("second")
    a.close()
    b.close()


def test_store_refuses_missing_volume_and_newer_schema(tmp_path):
    with pytest.raises(FileNotFoundError):
        Store(tmp_path / "missing")
    store = Store(tmp_path)
    store.close()
    db = sqlite3.connect(tmp_path / "ai_ops.sqlite")
    db.execute("PRAGMA user_version=99")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(tmp_path)


def test_isolated_health_endpoint_exposes_only_bounded_status(tmp_path):
    ready = queue.Queue()
    stop = threading.Event()

    def serve():
        store = Store(tmp_path)
        server = HTTPServer(("127.0.0.1", 0), create_handler(store))
        server.timeout = 0.1
        ready.put(server.server_port)
        try:
            while not stop.is_set():
                server.handle_request()
        finally:
            server.server_close()
            store.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        root = f"http://127.0.0.1:{ready.get(timeout=2)}"
        with urlopen(root + "/ready") as response:
            assert response.status == 200
            assert response.read() == b'{"schema":1,"integrity":"ok","pending":0}'
        with pytest.raises(HTTPError) as error:
            urlopen(root + "/tasks")
        assert error.value.code == 404
    finally:
        stop.set()
        thread.join(timeout=2)


def test_phase1_railway_restart_probe_preserves_idempotent_output(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("app.private_ops.store.time.time", lambda: clock[0])
    first = Store(tmp_path)
    assert run_phase1_probe(first) == "PHASE1_INITIALIZED_AWAITING_RESTART"
    first.close()
    clock[0] += 2
    second = Store(tmp_path)
    assert run_phase1_probe(second) == "PHASE1_RECOVERED_AWAITING_SECOND_RESTART"
    second.close()
    third = Store(tmp_path)
    assert run_phase1_probe(third) == "PHASE1_SECOND_RESTART_VERIFIED"
    third.close()
    fourth = Store(tmp_path)
    assert run_phase1_probe(fourth) == "PHASE1_VERIFIED_STABLE"
    fourth.close()
