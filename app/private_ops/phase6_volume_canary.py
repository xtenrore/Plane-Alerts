"""Restore a sanitized two-review shadow checkpoint on the dedicated volume.

The source is the owner-private DR repository. This deliberately uses a separate
shadow Store so no canary finding or review can enter the live scheduler queue.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from .dr_export import restore_empty
from .store import SCHEMA_VERSION, Store


class Repository(Protocol):
    def get(self, path: str) -> bytes: ...


def _validate(store: Store, source_commit: str) -> None:
    if store.health()["schema"] != SCHEMA_VERSION or store.health()["integrity"] != "ok":
        raise RuntimeError("shadow review checkpoint integrity failed")
    packets = store.db.execute("SELECT packet_id FROM ai_ops_evidence").fetchall()
    reviews = store.db.execute("""SELECT packet_id,role,provider,model,independence,validation_status
        FROM ai_ops_reviews ORDER BY created,review_id""").fetchall()
    if len(packets) != 1 or len(reviews) != 2:
        raise RuntimeError("shadow checkpoint requires one packet and two reviews")
    first, second = reviews
    if (first[0] != packets[0][0] or second[0] != first[0] or
            first[1] != "triage" or second[1] != "independent_review" or
            first[2] == second[2] or first[3] == second[3] or
            first[5] != "VALID" or second[5] != "VALID" or
            second[4] != "DIFFERENT_PROVIDER_AND_MODEL_BLIND"):
        raise RuntimeError("shadow reviewer independence or validation failed")
    case = store.db.execute("SELECT state,validation_status FROM ai_ops_cases WHERE packet_id=?", (packets[0][0],)).fetchone()
    if not case or case[0] != "INVESTIGATING" or case[1] != "VALID":
        raise RuntimeError("shadow case is not complete")


def run(data_dir: str | Path, repository: Repository, *, source_commit: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("exact source commit required")
    root = Path(data_dir) / "phase6-shadow-canary"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise RuntimeError("shadow checkpoint directory cannot be a symlink")
    destination = root / source_commit
    if destination.is_symlink():
        raise RuntimeError("shadow checkpoint cannot be a symlink")
    if not destination.exists():
        path = "private-ai-ops/snapshots/phase6-canary-" + source_commit + ".json"
        raw = repository.get(path)
        if len(raw) > 100000:
            raise ValueError("shadow checkpoint exceeds bound")
        payload = json.loads(raw)
        if (payload.get("schema") != 1 or payload.get("source_commit") != source_commit or
                not re.fullmatch(r"[0-9a-f]{40}", payload.get("evidence_commit", ""))):
            raise ValueError("shadow checkpoint identity mismatch")
        record = json.dumps(payload["snapshot"], sort_keys=True, separators=(",", ":")).encode()
        temporary = Path(tempfile.mkdtemp(prefix="incoming-", dir=root))
        try:
            restored = restore_empty(temporary, record, payload["manifest"])
            try:
                _validate(restored, source_commit)
            finally:
                restored.close()
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    reopened = Store(destination)
    try:
        _validate(reopened, source_commit)
    finally:
        reopened.close()
    return "PHASE6_CANARY_VOLUME_REOPEN_VERIFIED reviews=2 source_commit=" + source_commit
