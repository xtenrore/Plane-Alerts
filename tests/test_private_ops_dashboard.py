"""Phase 8A private authenticated operations GUI acceptance tests."""
from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

from app.private_ops.dashboard import AuthState, create_dashboard_handler
from app.private_ops.store import Store


def _request(port, method, path, *, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    raw = None if body is None else json.dumps(body).encode()
    h = dict(headers or {})
    if raw is not None:
        h["Content-Type"] = "application/json"
        h["Content-Length"] = str(len(raw))
    conn.request(method, path, body=raw, headers=h)
    response = conn.getresponse()
    data = response.read()
    all_headers = response.getheaders()
    status = response.status
    conn.close()
    return status, all_headers, data


def _header(headers, name):
    return [value for key, value in headers if key.lower() == name.lower()]


def _serve(tmp_path):
    bootstrap = Store(tmp_path)
    now = time.time()
    bootstrap.db.execute(
        "INSERT INTO ai_ops_jobs(id,status,priority,created,updated,lease_owner,lease_until) "
        "VALUES('task:active','RUNNING',5,?,?, 'worker-1', ?)",
        (now - 10, now, now + 120),
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_steps(job_id,step_id,status,input_hash,attempts,lease_owner,lease_until) "
        "VALUES('task:active','triage','RUNNING','abc',1,'worker-1',?)",
        (now + 120,),
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_attempts(job_id,step_id,worker,started) "
        "VALUES('task:active','triage','worker-1',?)", (now - 10,)
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_provider_health(slot,provider,attempts,successes,failures,"
        "consecutive_failures,cooldown_until,last_status,updated) "
        "VALUES('Slot 1','cloudflare',3,2,1,1,?,'rate_limited',?)",
        (now + 60, now),
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_provider_health(slot,provider,attempts,successes,failures,"
        "consecutive_failures,cooldown_until,last_status,updated) "
        "VALUES('Slot 2','cloudflare',2,2,0,0,0,'healthy',?)", (now,)
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_usage(batch_id,packet_id,task_role,provider,model,key_slot,"
        "latency_ms,success,failure_kind,malformed,input_tokens,output_tokens,created) "
        "VALUES(NULL,NULL,'supervisor','cloudflare','model-a','Slot 2',120,1,NULL,0,10,5,?)",
        (now,),
    )
    bootstrap.db.execute(
        "INSERT INTO ai_ops_tool_operations(operation_id,job_id,tool,arguments_json,input_hash,"
        "source_commit,sandbox_id,status,result_class,result_json,output_hash,created) "
        "VALUES('op:1','task:active','source_search','{}','x',?,'sandbox-op:1','SUCCEEDED',"
        "'OK',?, 'out',?)",
        ("a" * 40, json.dumps({"token": "must-not-leak", "latitude": 41.0, "summary": "safe"}), now),
    )
    bootstrap.close()

    handler = create_dashboard_handler(
        tmp_path / "ai_ops.sqlite",
        password="correct horse battery staple",
        require_https=False,
        session_ttl=300,
        stream_interval=0.02,
        stream_max_seconds=0.12,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _login(port, password="correct horse battery staple"):
    status, headers, body = _request(port, "GET", "/api/auth/bootstrap")
    assert status == 200
    csrf = json.loads(body)["csrf"]
    login_cookie = _header(headers, "Set-Cookie")[0].split(";", 1)[0]
    status, headers, body = _request(
        port, "POST", "/api/auth/login", body={"password": password},
        headers={"Cookie": login_cookie, "X-CSRF-Token": csrf},
    )
    return status, headers, body


def test_gui_requires_auth_and_successful_login_is_secure(tmp_path):
    server, thread = _serve(tmp_path)
    try:
        status, _, _ = _request(server.server_port, "GET", "/api/overview")
        assert status == 401
        status, _, _ = _login(server.server_port, "wrong")
        assert status == 401
        status, headers, body = _login(server.server_port)
        assert status == 200
        session = json.loads(body)
        assert "csrf" in session
        cookie = next(x for x in _header(headers, "Set-Cookie") if x.startswith("aiops_session="))
        lowered = cookie.lower()
        assert "httponly" in lowered and "secure" in lowered and "samesite=strict" in lowered
        assert "correct horse" not in body.decode()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_login_throttling_and_session_expiry(monkeypatch):
    state = AuthState("pw", session_ttl=300)
    for _ in range(6):
        assert state.login("198.51.100.10", "bad") is None
    with pytest.raises(PermissionError):
        state.login("198.51.100.10", "bad")
    clock = [1000.0]
    monkeypatch.setattr("app.private_ops.dashboard.time.time", lambda: clock[0])
    state = AuthState("pw", session_ttl=300)
    sid, _ = state.login("198.51.100.20", "pw")
    assert state.get(sid) is not None
    clock[0] += 301
    assert state.get(sid) is None


def test_csrf_logout_and_unauthorized_api(tmp_path):
    server, thread = _serve(tmp_path)
    try:
        status, headers, body = _login(server.server_port)
        assert status == 200
        session = json.loads(body)
        cookie = next(x for x in _header(headers, "Set-Cookie") if x.startswith("aiops_session=")).split(";", 1)[0]
        status, _, _ = _request(server.server_port, "POST", "/api/auth/logout", headers={"Cookie": cookie, "X-CSRF-Token": "wrong"})
        assert status == 403
        status, _, _ = _request(server.server_port, "POST", "/api/auth/logout", headers={"Cookie": cookie, "X-CSRF-Token": session["csrf"]})
        assert status == 200
        status, _, _ = _request(server.server_port, "GET", "/api/overview", headers={"Cookie": cookie})
        assert status == 401
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_real_state_provider_failover_and_secret_location_redaction(tmp_path):
    server, thread = _serve(tmp_path)
    try:
        status, headers, _ = _login(server.server_port)
        assert status == 200
        cookie = next(x for x in _header(headers, "Set-Cookie") if x.startswith("aiops_session=")).split(";", 1)[0]
        auth = {"Cookie": cookie}
        status, _, body = _request(server.server_port, "GET", "/api/agents", headers=auth)
        assert status == 200
        agents = json.loads(body)
        assert agents[0]["worker_id"] == "worker-1" and agents[0]["current_task"] == "task:active"
        status, _, body = _request(server.server_port, "GET", "/api/providers", headers=auth)
        providers = json.loads(body)
        assert [p["slot"] for p in providers] == ["Slot 1", "Slot 2"]
        assert providers[0]["last_status"] == "rate_limited" and providers[1]["last_status"] == "healthy"
        status, _, body = _request(server.server_port, "GET", "/api/engineering", headers=auth)
        text = body.decode()
        assert "must-not-leak" not in text and '"latitude":41' not in text and "[redacted]" in text and "safe" in text
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_large_task_list_is_bounded_and_mobile_states_exist(tmp_path):
    store = Store(tmp_path)
    for i in range(220):
        store.enqueue(f"bulk:{i}")
    store.close()
    handler = create_dashboard_handler(tmp_path / "ai_ops.sqlite", password="pw", require_https=False, stream_interval=0.02, stream_max_seconds=0.12)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler); server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        status, headers, _ = _login(server.server_port, "pw")
        assert status == 200
        cookie = next(x for x in _header(headers, "Set-Cookie") if x.startswith("aiops_session=")).split(";", 1)[0]
        status, _, body = _request(server.server_port, "GET", "/api/tasks?limit=9999", headers={"Cookie": cookie})
        assert status == 200 and len(json.loads(body)) == 200
        status, _, page = _request(server.server_port, "GET", "/", headers={"Cookie": cookie})
        html = page.decode()
        assert status == 200 and 'name="viewport"' in html and "@media(max-width:620px)" in html
        assert "STALE" in html and "Loading current durable state" in html and "No records in current durable state" in html and "Unable to load current state" in html
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_event_stream_emits_cursor_and_accepts_resume(tmp_path):
    server, thread = _serve(tmp_path)
    try:
        status, headers, _ = _login(server.server_port)
        cookie = next(x for x in _header(headers, "Set-Cookie") if x.startswith("aiops_session=")).split(";", 1)[0]
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        conn.request("GET", "/api/events/stream", headers={"Cookie": cookie})
        response = conn.getresponse(); assert response.status == 200
        first_id = None; deadline = time.time() + 1
        while time.time() < deadline:
            line = response.readline().decode().strip()
            if line.startswith("id:"):
                first_id = line.split(":", 1)[1].strip(); break
        assert first_id is not None; conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        conn.request("GET", "/api/events/stream", headers={"Cookie": cookie, "Last-Event-ID": first_id})
        response = conn.getresponse(); assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream"; conn.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
