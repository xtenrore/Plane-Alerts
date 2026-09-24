#!/usr/bin/env python3
"""Deterministic helpers for external Prediction Lab scheduled tasks.

This script never changes production prediction logic. It only moves repository
case files and advances checkpoint files after durable stage transitions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("prediction_lab")
STATE_SCHEMA = "plane-alerts-pipeline-checkpoint-v1"


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ValueError(f"Expected object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _checkpoint(task: str, case_id: str, event_id: str, count: int) -> None:
    path = ROOT / "state" / f"{task}.json"
    current = _load(path) if path.exists() else {"schema": STATE_SCHEMA, "task": task, "processed_count": 0}
    current.update({"schema": STATE_SCHEMA, "task": task, "last_success_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "last_case_id": case_id, "last_event_id": event_id, "processed_count": int(current.get("processed_count", 0) or 0) + count})
    _write(path, current)


def collect(limit: int) -> int:
    raw_files = sorted((ROOT / "raw").glob("*/*.json")); processed = 0; last_case = last_event = ""
    for source in raw_files:
        if processed >= limit: break
        event = _load(source); case_id = str(event.get("case_id") or ""); event_id = str(event.get("event_id") or "")
        if not case_id or not event_id: continue
        day = str(event.get("captured_at") or source.parent.name)[:10]; target = ROOT / "unchecked" / day / f"{case_id}.json"
        if target.exists():
            case = _load(target); event_ids = list(case.get("event_ids") or [])
            if event_id not in event_ids:
                event_ids.append(event_id); case["event_ids"] = sorted(event_ids); case["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"); _write(target, case)
        else:
            _write(target, {"schema": "plane-alerts-case-v1", "pipeline_stage": "unchecked", "case_id": case_id, "event_ids": [event_id], "first_event_kind": event.get("kind"), "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "source_digest": hashlib.sha256(source.read_bytes()).hexdigest(), "review": None})
        processed += 1; last_case, last_event = case_id, event_id
    if processed: _checkpoint("collector", last_case, last_event, processed)
    return processed


def mark_reviewed(case_path: str, disposition: str, evidence: str) -> Path:
    source = Path(case_path); case = _load(source); case_id = str(case.get("case_id") or "")
    if not case_id: raise ValueError("case_id missing")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"); case["pipeline_stage"] = "reviewed"; case["review"] = {"disposition": disposition, "evidence": evidence, "reviewed_at": now}
    day = source.parent.name if len(source.parent.name) == 10 else now[:10]; target = ROOT / "reviewed" / day / f"{case_id}.json"; _write(target, case); source.unlink(missing_ok=True)
    event_ids = list(case.get("event_ids") or []); _checkpoint("investigator", case_id, str(event_ids[-1] if event_ids else ""), 1); return target


def main() -> int:
    global ROOT
    parser = argparse.ArgumentParser(); parser.add_argument("--root", default="prediction_lab", help="Prediction Lab repository root"); sub = parser.add_subparsers(dest="command", required=True)
    collect_p = sub.add_parser("collect"); collect_p.add_argument("--limit", type=int, default=100)
    review_p = sub.add_parser("review"); review_p.add_argument("case_path"); review_p.add_argument("--disposition", required=True); review_p.add_argument("--evidence", required=True)
    args = parser.parse_args(); ROOT = Path(args.root)
    if args.command == "collect": print(collect(max(1, min(args.limit, 1000)))); return 0
    print(mark_reviewed(args.case_path, args.disposition, args.evidence)); return 0


if __name__ == "__main__": raise SystemExit(main())
