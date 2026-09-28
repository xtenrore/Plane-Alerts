"""Opt-in health process for the isolated Private Operations service.

No scheduler is enabled by this process. Provider inference remains disabled unless
a later phase explicitly projects credentials and enables a scheduler. Phase probes
are bounded one-shot verification hooks for the isolated service only.
"""
from __future__ import annotations

import json
import os
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .store import SCHEMA_VERSION, Store
from .phase1_probe import run as run_phase1_probe
from .dr_sync import sync_once
from .github_dr import GitHubDR
from .phase2_probe import run as run_phase2_probe
from .phase5_probe import run as run_phase5_probe
from .phase6_dr_probe import run as run_phase6_dr_probe
from .phase6_volume_canary import run as run_phase6_volume_canary


def create_handler(store: Store) -> type[BaseHTTPRequestHandler]:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in ("/health", "/ready"):
                self.send_error(404)
                return
            try:
                status = store.health()
                ready = status["integrity"] == "ok" and status["schema"] == SCHEMA_VERSION
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
            return

    return HealthHandler


def main() -> None:
    if os.environ.get("PRIVATE_AI_OPS_ENABLED", "").lower() != "true":
        raise RuntimeError("Private Operations must be explicitly enabled")
    data_dir = os.environ.get("AI_OPS_DATA_DIR", "")
    if not data_dir or not os.path.isabs(data_dir):
        raise RuntimeError("AI_OPS_DATA_DIR must be an absolute persistent directory")
    store = Store(data_dir)
    stop_backup = threading.Event()
    backup_thread: threading.Thread | None = None
    try:
        if os.environ.get("AI_OPS_PHASE1_PROBE_ENABLED", "").lower() == "true":
            print(run_phase1_probe(store), flush=True)
        if os.environ.get("AI_OPS_PHASE5_SHADOW_PROBE_ENABLED", "").lower() == "true":
            # The probe copies already-sanitized durable evidence to a temporary
            # store and uses deterministic fake providers. It cannot spend quota,
            # mutate source evidence, or start an inference scheduler.
            print(run_phase5_probe(store), flush=True)
    except BaseException:
        store.close()
        raise
    server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), create_handler(store))

    if os.environ.get("AI_OPS_GITHUB_DR_ENABLED", "").lower() == "true":
        token = os.environ.get("AI_OPS_GITHUB_DR_TOKEN", "")
        commit = os.environ.get("AI_OPS_SOURCE_COMMIT", "")
        if not token or not re.fullmatch(r"[0-9a-f]{40}", commit):
            store.close()
            raise RuntimeError("GitHub DR enabled without GitHub-controlled credentials or source commit")

        canary_commit = os.environ.get("AI_OPS_PHASE6_CANARY_SHA", "")
        if canary_commit:
            print(run_phase6_volume_canary(data_dir, GitHubDR(token), source_commit=canary_commit), flush=True)

        if os.environ.get("AI_OPS_PHASE2_FAULT_PROBE_ENABLED", "").lower() == "true":
            print(run_phase2_probe(store, GitHubDR(token), source_commit=commit), flush=True)
        if os.environ.get("AI_OPS_PHASE6_DR_PROBE_ENABLED", "").lower() == "true":
            print(run_phase6_dr_probe(store, GitHubDR(token), source_commit=commit), flush=True)

        def backup_loop() -> None:
            backup_store = Store(data_dir)
            repo = GitHubDR(token)
            try:
                while not stop_backup.is_set():
                    try:
                        sync_once(backup_store, repo, worker="github-dr", source_commit=commit)
                    except Exception:
                        print("AI_OPS_GITHUB_DR_RETRY_SCHEDULED", flush=True)
                    stop_backup.wait(30)
            finally:
                backup_store.close()

        backup_thread = threading.Thread(target=backup_loop, name="github-dr", daemon=True)
        backup_thread.start()

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stop_backup.set()
        server.server_close()
        if backup_thread is not None:
            backup_thread.join(timeout=10)
        store.close()
        print("AI_OPS_SHUTDOWN_CHECKPOINTED", flush=True)


if __name__ == "__main__":
    main()
