"""Opt-in health process for the isolated Private Operations service.

Phase 1 deliberately has no scheduler and no provider calls. It refuses to
start unless an existing, writable, dedicated persistent directory is set.
"""
from __future__ import annotations

import json
import os
import signal
from http.server import BaseHTTPRequestHandler, HTTPServer

from .store import Store
from .phase1_probe import run as run_phase1_probe


def create_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in ("/health", "/ready"):
                self.send_error(404)
                return
            try:
                status = store.health()
                ready = status["integrity"] == "ok" and status["schema"] == 1
            except Exception:
                status = {"status": "unavailable"}
                ready = False
            body = json.dumps(status, separators=(",", ":")).encode()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Avoid URL and header contents in logs, including query strings.
            return

    return HealthHandler


def main() -> None:
    if os.environ.get("PRIVATE_AI_OPS_ENABLED", "").lower() != "true":
        raise RuntimeError("Private Operations must be explicitly enabled")
    data_dir = os.environ.get("AI_OPS_DATA_DIR", "")
    if not data_dir or not os.path.isabs(data_dir):
        raise RuntimeError("AI_OPS_DATA_DIR must be an absolute persistent directory")
    store = Store(data_dir)
    try:
        if os.environ.get("AI_OPS_PHASE1_PROBE_ENABLED", "").lower() == "true":
            print(run_phase1_probe(store), flush=True)
    except BaseException:
        store.close()
        raise
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), create_handler(store))

    def stop(_signum: int, _frame: object) -> None:
        # shutdown() must run from a different thread than serve_forever().
        import threading
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        store.close()
        print("AI_OPS_SHUTDOWN_CHECKPOINTED", flush=True)


if __name__ == "__main__":
    main()
