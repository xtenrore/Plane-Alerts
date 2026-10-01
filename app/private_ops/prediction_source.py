"""Bounded read-only reader for an existing Prediction Lab evidence checkout.

The checkout comes from the prediction-lab-data branch, which is synced
independently of the live monitor. Caller must separately prove the upstream
sync has completed the requested closed hour before advancing a checkpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .collector import adapt_prediction_lab_record

MAX_FILES_PER_DAY = 32000
MAX_FILE_BYTES = 16_000_000
MAX_SOURCE_EVENTS_PER_HOUR = 2048


def read_hour(root: Path, start: int, end: int) -> Iterator[dict]:
    if end - start != 3600 or start % 3600:
        raise ValueError("closed UTC hour required")
    dates = {datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d"),
             datetime.fromtimestamp(end - 1, timezone.utc).strftime("%Y-%m-%d")}
    seen: dict[str, dict] = {}
    scanned = 0
    for date in sorted(dates):
        directory = root / "prediction_lab" / "raw" / date
        if not directory.is_dir():
            raise FileNotFoundError("Prediction Lab evidence date is missing")
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix not in (".json", ".ndjson"):
                continue
            scanned += 1
            if scanned > MAX_FILES_PER_DAY:
                raise ValueError("source file count exceeds audit bound")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("source evidence file too large")
            with path.open(encoding="utf-8") as handle:
                lines = handle if path.suffix == ".ndjson" else [handle.read()]
                for line in lines:
                    if not line.strip():
                        continue
                    raw = json.loads(line)
                    if not isinstance(raw, dict) or raw.get("kind") not in ("prediction", "outcome"):
                        continue
                    event = adapt_prediction_lab_record(raw)
                    if not start <= event["at"] < end:
                        continue
                    if event["event_id"] in seen:
                        if seen[event["event_id"]] != event:
                            raise ValueError("conflicting duplicate source evidence")
                        continue
                    if len(seen) >= MAX_SOURCE_EVENTS_PER_HOUR:
                        raise ValueError("source event count exceeds audit bound")
                    seen[event["event_id"]] = event
                    yield event
