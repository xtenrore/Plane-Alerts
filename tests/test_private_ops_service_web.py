"""Phase 8A production wrapper security regressions."""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from app.private_ops.service import create_private_web_handler
from app.private_ops.store import Store


def _request(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("GET", path)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    conn.close()
    return status, data


def test_public_health_is_readiness_only_when_private_gui_enabled(tmp_path):
    store = Store(tmp_path)
    store.enqueue("sensitive-operational-task")
    store.close()
    handler = create_private_web_handler(
        tmp_path / "ai_ops.sqlite", password="owner-password", require_https=False
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/health", "/ready"):
            status, body = _request(server.server_port, path)
            assert status == 200
            assert json.loads(body) == {"status": "ready"}
            assert b"pending" not in body
            assert b"finding" not in body
            assert b"schema" not in body
            assert b"sensitive-operational-task" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
