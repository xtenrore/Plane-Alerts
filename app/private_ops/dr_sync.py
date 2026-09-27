"""Durable, debounced GitHub DR sync with read-back verification.

Transport receives only sanitized bytes. An unavailable destination cannot
block local jobs; a failed attempt retains its revision for later retry.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Protocol

from .dr_export import export
from .store import Store


class Repository(Protocol):
    def put(self, path: str, content: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...


def sync_once(store: Store, repo: Repository, *, worker: str, source_commit: str,
              now: float | None = None, force: bool = False) -> bool:
    """Attempt one revision. False means no work or another worker owns it."""
    now = time.time() if now is None else now
    if not worker or len(worker) > 128:
        raise ValueError("valid worker required")
    with store.transaction() as db:
        row = db.execute("SELECT * FROM ai_ops_dr_sync WHERE destination='github'").fetchone()
        if not row or row["revision"] <= row["synced_revision"] or (not force and row["next_attempt"] > now):
            return False
        if row["lease_until"] is not None and row["lease_until"] > now:
            return False
        result = db.execute("""UPDATE ai_ops_dr_sync SET lease_owner=?, lease_until=?, last_attempt=?
            WHERE destination='github' AND (lease_until IS NULL OR lease_until<=?)""",
            (worker, now + 300, now, now))
        if result.rowcount != 1:
            return False
    try:
        # Snapshot under a short SQLite write lock; the network operation is
        # outside that lock. New revisions remain pending after this sync.
        with store.transaction() as db:
            revision = db.execute("SELECT revision FROM ai_ops_dr_sync WHERE destination='github'").fetchone()[0]
            data, manifest = export(store, source_commit=source_commit)
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(data).hexdigest()
        base = f"private-ai-ops/snapshots/{digest}"
        for path, expected in ((base + ".json", data), (base + ".manifest.json", manifest_bytes)):
            repo.put(path, expected)
            actual = repo.get(path)
            if actual != expected or hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
                raise IOError("GitHub DR read-back hash mismatch")
        # An immutable hash-addressed manifest serves as the verified remote
        # checkpoint. No mutable pointer or unrelated repository file touched.
        with store.transaction() as db:
            result = db.execute("""UPDATE ai_ops_dr_sync SET synced_revision=?,last_verified=?,last_hash=?,
                retry_count=0,next_attempt=0,lease_owner=NULL,lease_until=NULL
                WHERE destination='github' AND lease_owner=? AND lease_until>?""",
                (revision, now, digest, worker, now))
            if result.rowcount != 1:
                raise RuntimeError("GitHub DR sync lease expired")
        return True
    except Exception:
        with store.transaction() as db:
            row = db.execute("SELECT retry_count FROM ai_ops_dr_sync WHERE destination='github' AND lease_owner=?", (worker,)).fetchone()
            if row:
                retries = row[0] + 1
                db.execute("""UPDATE ai_ops_dr_sync SET retry_count=?,next_attempt=?,lease_owner=NULL,
                    lease_until=NULL WHERE destination='github' AND lease_owner=?""",
                    (retries, now + min(3600, 30 * (2 ** min(retries, 7))), worker))
        raise


def status(store: Store, destination: str) -> dict[str, object]:
    if destination not in ("github", "dropbox"):
        raise ValueError("unknown backup destination")
    row = store.db.execute("SELECT * FROM ai_ops_dr_sync WHERE destination=?", (destination,)).fetchone()
    return {"destination": destination, "revision": row["revision"], "synced_revision": row["synced_revision"],
            "last_attempt": row["last_attempt"], "last_verified": row["last_verified"],
            "last_hash": row["last_hash"], "retry_count": row["retry_count"],
            "outcome": "backed_up" if row["last_verified"] is not None and row["synced_revision"] == row["revision"]
            else "failed" if row["retry_count"] else "pending"}
