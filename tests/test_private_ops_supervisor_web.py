"""Phase 8B authenticated Supervisor Chat web acceptance tests."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from app.private_ops.dashboard import create_dashboard_handler
from app.private_ops.provider_adapters import Slot
from app.private_ops.store import Store
from app.private_ops.supervisor import ChatResult, SupervisorBackend, SupervisorEngine, SupervisorStore
from app.private_ops.supervisor_web import extend_private_handler


class FakeTransport:
    def __init__(self):
        self.calls = []

    def chat(self, slot, model, messages, *, tools=(), affinity=""):
        self.calls.append((slot.provider, slot.name, model, affinity))
        return ChatResult("Current durable backend state shows task:live.", (), 30, 8)


def _request(port, method, path, *, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    raw = None if body is None else json.dumps(body).encode()
    request_headers = dict(headers or {})
    if raw is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Content-Length"] = str(len(raw))
    conn.request(method, path, body=raw, headers=request_headers)
    response = conn.getresponse()
    data = response.read()
    result = response.status, response.getheaders(), data
    conn.close()
    return result


def _header(headers, name):
    return [value for key, value in headers if key.lower() == name.lower()]


def _login(port):
    status, headers, body = _request(port, "GET", "/api/auth/bootstrap")
    assert status == 200
    csrf = json.loads(body)["csrf"]
    login_cookie = _header(headers, "Set-Cookie")[0].split(";", 1)[0]
    status, headers, body = _request(
        port, "POST", "/api/auth/login",
        body={"password": "owner-password"},
        headers={"Cookie": login_cookie, "X-CSRF-Token": csrf},
    )
    assert status == 200
    session = json.loads(body)
    cookie = next(value for value in _header(headers, "Set-Cookie") if value.startswith("aiops_session=")).split(";", 1)[0]
    return cookie, session["csrf"]


def _serve(tmp_path):
    store = Store(tmp_path)
    store.enqueue("task:live")
    store.close()
    backend = SupervisorBackend(tmp_path / "ai_ops.sqlite")
    state = SupervisorStore(tmp_path)
    transport = FakeTransport()
    slots = [
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "credential-one", "a" * 32),
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN_2", "credential-two", "b" * 32),
    ]
    engine = SupervisorEngine(
        backend, state, slots,
        cloudflare_model="@cf/zai-org/glm-4.7-flash",
        transport=transport,
    )
    base = create_dashboard_handler(
        tmp_path / "ai_ops.sqlite", password="owner-password", require_https=False,
        stream_interval=0.02, stream_max_seconds=0.12,
    )
    handler = extend_private_handler(base, engine)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, engine, transport


def test_supervisor_chat_requires_owner_session_and_csrf_and_persists(tmp_path):
    server, thread, engine, transport = _serve(tmp_path)
    try:
        status, _, page = _request(server.server_port, "GET", "/supervisor")
        assert status == 200 and b"Supervisor Chat" in page and b"read-only operations chat" in page
        status, _, _ = _request(server.server_port, "GET", "/api/supervisor/status")
        assert status == 401

        cookie, csrf = _login(server.server_port)
        auth = {"Cookie": cookie}
        status, _, raw = _request(server.server_port, "GET", "/api/supervisor/status", headers=auth)
        status_payload = json.loads(raw)
        assert status == 200 and status_payload["primary_provider"] == "cloudflare"
        assert status_payload["authority"] == "read_only_operations"
        assert "credential" not in raw.decode().lower()

        status, _, _ = _request(
            server.server_port, "POST", "/api/supervisor/conversations", body={},
            headers={"Cookie": cookie, "X-CSRF-Token": "wrong"},
        )
        assert status == 403
        status, _, raw = _request(
            server.server_port, "POST", "/api/supervisor/conversations", body={},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        assert status == 201
        cid = json.loads(raw)["conversation_id"]

        status, _, raw = _request(
            server.server_port, "POST", "/api/supervisor/chat",
            body={"conversation_id": cid, "message": "What is active right now?"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        result = json.loads(raw)
        assert status == 200 and result["status"] == "OK"
        assert result["provider"] == "cloudflare" and result["slot"] == "Slot 1"
        assert transport.calls and transport.calls[0][3] == cid
        assert "CLOUDFLARE_API_TOKEN" not in raw.decode()

        status, _, raw = _request(
            server.server_port, "GET", "/api/supervisor/history?conversation_id=" + cid,
            headers=auth,
        )
        history = json.loads(raw)
        assert status == 200
        assert [item["role"] for item in history["messages"]] == ["user", "assistant"]
        assert history["conversation"]["slot"] == "Slot 1"
        reopened = SupervisorStore(tmp_path)
        assert reopened.messages(cid)[-1]["content"] == "Current durable backend state shows task:live."
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_secret_like_chat_is_rejected_before_any_provider_call(tmp_path):
    server, thread, _engine, transport = _serve(tmp_path)
    try:
        cookie, csrf = _login(server.server_port)
        status, _, raw = _request(
            server.server_port, "POST", "/api/supervisor/chat",
            body={"message": "password=do-not-send-this"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf},
        )
        payload = json.loads(raw)
        assert status == 200 and payload["status"] == "REJECTED"
        assert transport.calls == []
        assert "do-not-send-this" not in raw.decode()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
