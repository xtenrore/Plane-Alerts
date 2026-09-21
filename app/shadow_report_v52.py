"""Read-only v5.2 shadow evaluation report.

This command is operator tooling only. It never changes feature flags, model
selection, promotion state, or live alert behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.database import close_db, connect_db
from app.shadow_evaluation_v52 import (
    EVALUATION_COLLECTION,
    feature_flags,
    promotion_assessment,
    summarize_evaluations,
)
from app.version import PREDICTION_VERSION, VERSION

_MAX_ROWS = 2000


async def collect_shadow_report(db: Any, *, days: int = 7, limit: int = _MAX_ROWS) -> dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, min(30, int(days))))
    cursor = (
        db[EVALUATION_COLLECTION]
        .find({"evaluated_at": {"$gte": cutoff}}, {"user_id": 0})
        .sort("evaluated_at", -1)
        .limit(max(1, min(_MAX_ROWS, int(limit))))
    )
    rows = [dict(doc) async for doc in cursor]
    summary = summarize_evaluations(rows)

    by_release: dict[str, list[dict[str, Any]]] = {}
    for item in summary["models"]:
        by_release.setdefault(str(item["release_version"]), []).append(item)

    assessments: list[dict[str, Any]] = []
    for release, models in sorted(by_release.items()):
        control = next((item for item in models if item.get("model_role") == "production-control"), None)
        if control is None:
            continue
        for candidate in models:
            if candidate.get("model_role") != "shadow-candidate":
                continue
            assessments.append({
                "release_version": release,
                "candidate_model": candidate["model_id"],
                "control_model": control["model_id"],
                # Replay/live success are deliberately not inferred merely from
                # having rows. Promotion stays held until release engineering
                # supplies explicit evidence for both gates.
                "assessment": promotion_assessment(
                    candidate,
                    control,
                    replay_success=False,
                    live_shadow_success=False,
                ),
            })

    return {
        "version": VERSION,
        "prediction_version": PREDICTION_VERSION,
        "feature_flags": feature_flags(),
        "window_days": max(1, min(30, int(days))),
        "evaluation_rows": len(rows),
        "summary": summary,
        "promotion_assessments": assessments,
        "promotion_policy": (
            "No candidate is promoted by this command. Promotion requires sufficient representative samples, "
            "no major safety regression, replay success and live shadow success, followed by engineering review."
        ),
        "coverage_warning": (
            "Regional or missing ADS-B coverage is unresolved evidence and is never converted into a successful prediction or miss."
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="planealerts shadow-eval", description="Plane Alerts v5.2 shadow evaluation report")
    parser.add_argument("--days", type=int, default=7, help="Evaluation window in days (1-30)")
    parser.add_argument("--limit", type=int, default=_MAX_ROWS, help="Maximum evaluation rows to inspect (1-2000)")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    db = await connect_db(max_retries=1, retry_delay=0.0, timeout_ms=1500, ensure_indexes=False)
    try:
        return await collect_shadow_report(db, days=args.days, limit=args.limit)
    finally:
        await close_db()


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        payload = asyncio.run(_run(args))
    except Exception as exc:
        print(json.dumps({
            "status": "unavailable",
            "error": type(exc).__name__,
            "detail": "Shadow evaluation telemetry is unavailable; live Plane Alerts monitoring is independent.",
        }, indent=2))
        return 2
    print(json.dumps(payload, default=str, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
