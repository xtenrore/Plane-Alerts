#!/usr/bin/env python3
"""Deterministic helpers for external Prediction Lab scheduled tasks.

This script never changes production prediction logic. It only moves repository
case files and advances checkpoint files after durable stage transitions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path("prediction_lab")
STATE_SCHEMA = "plane-alerts-pipeline-checkpoint-v1"
CASE_STAGES = ("unchecked", "reviewed", "solved")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected object: {path}")
    return value


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _checkpoint(task: str, case_id: str, event_id: str, count: int) -> None:
    path = ROOT / "state" / f"{task}.json"
    current = _load(path) if path.exists() else {"schema": STATE_SCHEMA, "task": task, "processed_count": 0}
    current.update({
        "schema": STATE_SCHEMA,
        "task": task,
        "last_success_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_case_id": case_id,
        "last_event_id": event_id,
        "processed_count": int(current.get("processed_count", 0) or 0) + count,
    })
    _write(path, current)


def _case_in_stage(stage: str, case_id: str) -> Path | None:
    if stage not in CASE_STAGES:
        raise ValueError(f"Unsupported Prediction Lab stage: {stage}")
    matches = sorted((ROOT / stage).glob(f"*/{case_id}.json"))
    if len(matches) > 1:
        raise ValueError(f"Duplicate {stage} case paths for {case_id}")
    return matches[0] if matches else None


def _existing_case(case_id: str) -> Path | None:
    # A later lifecycle stage wins. This prevents immutable raw evidence from
    # recreating a case after it has already been reviewed or solved.
    for stage in ("solved", "reviewed", "unchecked"):
        path = _case_in_stage(stage, case_id)
        if path is not None:
            return path
    return None


def _require_stage(path: Path, stage: str) -> None:
    try:
        relative = path.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"Case path is outside Prediction Lab root: {path}") from exc
    if len(relative.parts) < 3 or relative.parts[0] != stage:
        raise ValueError(f"Expected case under {stage}: {path}")


def collect(limit: int) -> int:
    raw_files = sorted((ROOT / "raw").glob("*/*.json"))
    processed = 0
    last_case = last_event = ""
    for source in raw_files:
        if processed >= limit:
            break
        event = _load(source)
        case_id = str(event.get("case_id") or "")
        event_id = str(event.get("event_id") or "")
        if not case_id or not event_id:
            continue
        day = str(event.get("captured_at") or source.parent.name)[:10]
        existing = _existing_case(case_id)
        if existing is not None:
            case = _load(existing)
            event_ids = list(case.get("event_ids") or [])
            if event_id in event_ids:
                continue
            stage = str(case.get("pipeline_stage") or existing.parent.parent.name)
            if stage != "unchecked":
                # Reviewed and solved cases are durable history. New raw files
                # with the same case_id must not silently reopen/recreate them.
                # A genuine regression needs a distinct case_id.
                continue
            event_ids.append(event_id)
            case["event_ids"] = sorted(event_ids)
            case["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            _write(existing, case)
        else:
            target = ROOT / "unchecked" / day / f"{case_id}.json"
            _write(target, {
                "schema": "plane-alerts-case-v1",
                "pipeline_stage": "unchecked",
                "case_id": case_id,
                "event_ids": [event_id],
                "first_event_kind": event.get("kind"),
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "source_digest": hashlib.sha256(source.read_bytes()).hexdigest(),
                "review": None,
            })
        processed += 1
        last_case, last_event = case_id, event_id
    if processed:
        _checkpoint("collector", last_case, last_event, processed)
    return processed


def mark_reviewed(case_path: str, disposition: str, evidence: str) -> Path:
    source = Path(case_path)
    if not source.exists():
        existing = _case_in_stage("reviewed", source.stem)
        if existing is not None:
            return existing
        solved = _case_in_stage("solved", source.stem)
        if solved is not None:
            raise ValueError(f"Case is already solved: {solved}")
        raise FileNotFoundError(source)
    _require_stage(source, "unchecked")
    case = _load(source)
    case_id = str(case.get("case_id") or "")
    if not case_id:
        raise ValueError("case_id missing")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    case["pipeline_stage"] = "reviewed"
    case["review"] = {"disposition": disposition, "evidence": evidence, "reviewed_at": now}
    day = source.parent.name if len(source.parent.name) == 10 else now[:10]
    target = ROOT / "reviewed" / day / f"{case_id}.json"
    _write(target, case)
    source.unlink(missing_ok=True)
    event_ids = list(case.get("event_ids") or [])
    _checkpoint("investigator", case_id, str(event_ids[-1] if event_ids else ""), 1)
    return target


def mark_solved(
    case_path: str,
    fix_commit: str,
    deployed_version: str,
    verification_evidence: str,
    error_museum_refs: Iterable[str] = (),
) -> Path:
    source = Path(case_path)
    case_id_hint = source.stem
    existing_solved = _case_in_stage("solved", case_id_hint)
    if not source.exists():
        if existing_solved is None:
            raise FileNotFoundError(source)
        solved_case = _load(existing_solved)
        resolution = dict(solved_case.get("resolution") or {})
        if resolution.get("fix_commit_sha") != fix_commit or resolution.get("deployed_version") != deployed_version:
            raise ValueError(f"Solved case metadata conflicts with requested release: {existing_solved}")
        return existing_solved

    _require_stage(source, "reviewed")
    case = _load(source)
    case_id = str(case.get("case_id") or "")
    if not case_id:
        raise ValueError("case_id missing")
    if str(case.get("pipeline_stage") or "") != "reviewed":
        raise ValueError("Only reviewed cases can be marked solved")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", fix_commit):
        raise ValueError("fix_commit must be the exact 40-character Git commit SHA")
    if not deployed_version.strip():
        raise ValueError("deployed_version is required")
    if not verification_evidence.strip():
        raise ValueError("verification_evidence is required")
    if existing_solved is not None:
        raise ValueError(f"Solved case already exists: {existing_solved}")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    case["pipeline_stage"] = "solved"
    case["resolution"] = {
        "status": "production_verified",
        "fix_commit_sha": fix_commit.lower(),
        "deployed_version": deployed_version.strip(),
        "production_verified_at": now,
        "verification_evidence": verification_evidence.strip(),
        "error_museum_refs": sorted({ref.strip() for ref in error_museum_refs if ref.strip()}),
    }
    target = ROOT / "solved" / now[:10] / f"{case_id}.json"
    _write(target, case)
    source.unlink(missing_ok=True)
    event_ids = list(case.get("event_ids") or [])
    _checkpoint("release", case_id, str(event_ids[-1] if event_ids else ""), 1)
    return target


def main() -> int:
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="prediction_lab", help="Prediction Lab repository root")
    sub = parser.add_subparsers(dest="command", required=True)
    collect_p = sub.add_parser("collect")
    collect_p.add_argument("--limit", type=int, default=100)
    review_p = sub.add_parser("review")
    review_p.add_argument("case_path")
    review_p.add_argument("--disposition", required=True)
    review_p.add_argument("--evidence", required=True)
    solve_p = sub.add_parser("solve")
    solve_p.add_argument("case_path")
    solve_p.add_argument("--fix-commit", required=True)
    solve_p.add_argument("--deployed-version", required=True)
    solve_p.add_argument("--verification-evidence", required=True)
    solve_p.add_argument("--error-museum-ref", action="append", default=[])
    args = parser.parse_args()
    ROOT = Path(args.root)
    if args.command == "collect":
        print(collect(max(1, min(args.limit, 1000))))
        return 0
    if args.command == "review":
        print(mark_reviewed(args.case_path, args.disposition, args.evidence))
        return 0
    print(mark_solved(
        args.case_path,
        args.fix_commit,
        args.deployed_version,
        args.verification_evidence,
        args.error_museum_ref,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
