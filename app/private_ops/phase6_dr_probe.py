"""One-shot live-volume disaster-recovery roundtrip for the isolated service."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Protocol

from .dr_export import restore_empty
from .dr_sync import sync_once, status
from .store import SCHEMA_VERSION, Store


class Repository(Protocol):
    def get(self, path: str) -> bytes: ...
    def put(self, path: str, content: bytes) -> None: ...


def run(store: Store, repo: Repository, *, source_commit: str) -> str:
    """Force one sanitized snapshot and verify its remote bytes and clean restore.

    Never read an aircraft state store or a provider credential here. The only
    mutation to the live volume is the durable DR revision/checkpoint.
    """
    if store.health()["schema"] != SCHEMA_VERSION or store.health()["integrity"] != "ok":
        raise RuntimeError("live AI Ops volume is unhealthy")
    marker = store.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='phase1_probe'").fetchone()
    if not marker or marker[0] != "verified_extended":
        raise RuntimeError("Phase 1 stable marker is missing")
    with store.transaction() as db:
        store._mark_dr_dirty(db)
    if not sync_once(store, repo, worker="phase6-dr-probe", source_commit=source_commit, force=True):
        raise RuntimeError("live DR sync did not acquire its checkpoint")
    current = status(store, "github")
    if current["outcome"] != "backed_up" or not current["last_hash"]:
        raise RuntimeError("live DR checkpoint is not verified")
    prefix = "private-ai-ops/snapshots/" + str(current["last_hash"])
    data = repo.get(prefix + ".json")
    manifest = json.loads(repo.get(prefix + ".manifest.json"))
    if manifest.get("schema") != 4 or manifest.get("source_commit") != source_commit:
        raise RuntimeError("remote snapshot is not the Phase 6 source")
    with tempfile.TemporaryDirectory(prefix="ai-ops-phase6-dr-") as directory:
        root = Path(directory)
        recovered = restore_empty(root, data, manifest)
        try:
            if recovered.health()["integrity"] != "ok" or recovered.health()["schema"] != SCHEMA_VERSION:
                raise RuntimeError("clean restore failed schema or integrity")
            check = recovered.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='phase1_probe'").fetchone()
            if not check or check[0] != marker[0]:
                raise RuntimeError("stable marker missing from remote restore")
        finally:
            recovered.close()
    return "PHASE6_DR_LIVE_ROUNDTRIP_VERIFIED"
