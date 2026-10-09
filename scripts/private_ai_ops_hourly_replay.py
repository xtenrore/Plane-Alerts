"""Replay a closed Prediction Lab hour with the isolated deterministic collector."""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.private_ops.collector import collect
from app.private_ops.dr_export import export, restore_empty
from app.private_ops.prediction_source import read_hour
from app.private_ops.store import Store


def replay(source: Path, hour: str, source_commit: str, output: Path) -> tuple[int, int, str]:
    parsed = datetime.fromisoformat(hour.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.minute or parsed.second or parsed.microsecond:
        raise ValueError("UTC hour required")
    start = int(parsed.astimezone(timezone.utc).timestamp())
    with tempfile.TemporaryDirectory(prefix="ai-ops-hour-") as directory:
        root = Path(directory)
        store = Store(root)
        try:
            result = collect(store, start=start, end=start + 3600,
                             records=read_hour(source, start, start + 3600),
                             source_complete=True)
            data, manifest = export(store, source_commit=source_commit)
            clean = root / "clean"
            clean.mkdir()
            restored = restore_empty(clean, data, manifest)
            try:
                assert restored.health()["integrity"] == "ok"
                assert restored.db.execute("SELECT count(*) FROM ai_ops_evidence").fetchone()[0] == result.packets
            finally:
                restored.close()
        finally:
            store.close()
    payload = {"schema": 1, "window_start": start, "window_end": start+3600,
               "snapshot_manifest": manifest, "snapshot": json.loads(data)}
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(serialized) > 2_100_000:
        raise ValueError("hourly replay artifact exceeds bound")
    output.write_bytes(serialized)
    return result.events, result.packets, hashlib.sha256(serialized).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--hour", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    events, packets, digest = replay(args.source, args.hour, args.source_commit, args.output)
    print(f"PHASE4_SHADOW_REPLAY events={events} packets={packets} sha256={digest}")
