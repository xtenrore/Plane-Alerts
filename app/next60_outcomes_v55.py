"""Coverage-aware resolver for file-backed /next60 shadow expectations.

Expectation evidence is already sanitized into the persistent Prediction Lab spool.
This resolver runs outside the five-second monitor, maps the private observer hash
back to live Mongo configuration without writing coordinates to repository evidence,
and records subsequent reality only when coverage is sufficient. Missing ADS-B
coverage is always inconclusive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.forecast_outcomes import OUTCOME_VERSION, observed_outcome
from app.prediction_lab_files_v55 import append_evidence, ensure_layout, observer_ref, root_path

logger = logging.getLogger(__name__)

RESOLVE_GRACE_S = 900.0
INCONCLUSIVE_AFTER_S = 2700.0
MAX_EXPECTATION_FILES = 800
MAX_CASES_PER_RUN = 40
STATE_FILE = "next60_outcomes_v55.json"
STATE_MAX_RESOLVED = 4096


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _state_path(root: Path) -> Path:
    return root / "state" / STATE_FILE


def _read_state(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_state_path(root).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {"resolved": {}}
    if not isinstance(value, dict):
        return {"resolved": {}}
    resolved = value.get("resolved")
    if not isinstance(resolved, dict):
        value["resolved"] = {}
    return value


def _atomic_state(root: Path, state: dict[str, Any]) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _recent_expectation_paths(root: Path, now: datetime) -> list[Path]:
    raw = root / "raw"
    paths: list[Path] = []
    for offset in range(0, 3):
        day = (now.date() - timedelta(days=offset)).isoformat()
        directory = raw / day
        if not directory.exists():
            continue
        day_paths = [*directory.glob("*.json"), *directory.glob("*.synced")]
        day_paths.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0.0, reverse=True)
        paths.extend(day_paths[:MAX_EXPECTATION_FILES])
        if len(paths) >= MAX_EXPECTATION_FILES:
            break
    return paths[:MAX_EXPECTATION_FILES]


def _load_expectations(root: Path, now: datetime, resolved: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _recent_expectation_paths(root, now):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(doc, dict) or doc.get("kind") != "next60_expectation":
            continue
        event_id = str(doc.get("event_id") or "")
        if not event_id or event_id in resolved:
            continue
        window_end = _utc(doc.get("window_end"))
        if window_end is None or (now - window_end).total_seconds() < RESOLVE_GRACE_S:
            continue
        rows.append(doc)
        if len(rows) >= MAX_CASES_PER_RUN:
            break
    return rows


async def _observer_locations(db: Any, root: Path) -> dict[str, dict[str, Any]]:
    ids: list[int] = []
    cursor = db["users"].find({"setup_complete": True}, {"user_id": 1, "_id": 0})
    async for doc in cursor:
        try:
            ids.append(int(doc["user_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not ids:
        return {}
    result: dict[str, dict[str, Any]] = {}
    cursor = db["locations"].find(
        {"user_id": {"$in": ids}},
        {"user_id": 1, "latitude": 1, "longitude": 1, "radius_km": 1, "_id": 0},
    )
    async for loc in cursor:
        try:
            user_id = int(loc["user_id"])
            result[observer_ref(user_id, root)] = {
                "user_id": user_id,
                "latitude": float(loc["latitude"]),
                "longitude": float(loc["longitude"]),
                "radius_km": float(loc.get("radius_km") or 15.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _expectation_for_scoring(doc: dict[str, Any]) -> dict[str, Any] | None:
    window_start = _utc(doc.get("window_start"))
    window_end = _utc(doc.get("window_end"))
    predicted = _utc(doc.get("predicted_cpa_at"))
    if window_start is None or window_end is None or predicted is None:
        return None
    return {
        "window_start": window_start,
        "window_end": window_end,
        "predicted_cpa_at": predicted,
    }


async def _route_for(db: Any, callsign: str, utc_date: str) -> dict[str, Any] | None:
    return await db["flight_route_samples"].find_one(
        {"callsign": callsign, "utc_date": utc_date},
        {"points": 1, "aircraft_type": 1, "_id": 0},
    )


async def resolve_next60_outcomes(db: Any, *, now: datetime | None = None, root: Path | None = None) -> dict[str, int]:
    """Resolve a bounded batch of matured /next60 expectations.

    The routine is safe to run repeatedly. An expectation is checkpointed only
after its outcome evidence has been atomically written to the file spool.
    """
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    base = ensure_layout(root or root_path())
    state = await asyncio.to_thread(_read_state, base)
    resolved_map = state.get("resolved") if isinstance(state.get("resolved"), dict) else {}
    resolved_ids = set(str(key) for key in resolved_map)
    expectations = await asyncio.to_thread(_load_expectations, base, current, resolved_ids)
    if not expectations:
        return {"examined": 0, "resolved": 0, "scoreable": 0, "inconclusive": 0}

    observers = await _observer_locations(db, base)
    counters = {"examined": 0, "resolved": 0, "scoreable": 0, "inconclusive": 0}
    changed = False

    for expectation in expectations:
        counters["examined"] += 1
        event_id = str(expectation.get("event_id") or "")
        observer = observers.get(str(expectation.get("observer_ref") or ""))
        scoring = _expectation_for_scoring(expectation)
        callsign = str(expectation.get("callsign") or "").strip().upper()
        window_end = _utc(expectation.get("window_end"))
        if not event_id or scoring is None or not callsign or window_end is None:
            continue

        age_after_window = (current - window_end).total_seconds()
        route = None
        evidence: dict[str, Any] | None = None
        if observer is not None:
            route = await _route_for(db, callsign, scoring["predicted_cpa_at"].date().isoformat())
            points = list((route or {}).get("points") or [])
            if points:
                evidence = observed_outcome(
                    points,
                    observer["latitude"],
                    observer["longitude"],
                    scoring,
                    observer["radius_km"],
                )

        if evidence is not None and bool(evidence.get("scoreable")):
            actual_pass = bool(evidence.get("actual_pass"))
            coverage_resolution = "observed_positive" if actual_pass else "coverage_proven_negative"
            scoreable = True
        elif age_after_window >= INCONCLUSIVE_AFTER_S:
            actual_pass = None
            coverage_resolution = "inconclusive"
            scoreable = False
        else:
            continue

        closest = evidence.get("closest") if isinstance(evidence, dict) else None
        actual_closest_km = float(closest[0]) if isinstance(closest, tuple) and len(closest) == 2 else None
        actual_cpa_at = datetime.fromtimestamp(float(closest[1]), timezone.utc) if isinstance(closest, tuple) and len(closest) == 2 else None
        predicted_at = scoring["predicted_cpa_at"]
        timing_scoreable = bool((evidence or {}).get("timing_scoreable")) and actual_cpa_at is not None
        timing_error_s = (actual_cpa_at - predicted_at).total_seconds() if timing_scoreable else None

        outcome_doc = {
            "kind": "next60_outcome",
            "captured_at": current,
            "expectation_event_id": event_id,
            "expectation_case_id": expectation.get("case_id"),
            "observer_ref": expectation.get("observer_ref"),
            "callsign": callsign,
            "aircraft_type": (route or {}).get("aircraft_type") or expectation.get("aircraft_type"),
            "prediction_horizon_s": expectation.get("prediction_horizon_s"),
            "predicted_cpa_at": predicted_at,
            "predicted_closest_km": expectation.get("predicted_closest_km"),
            "actual_pass": actual_pass,
            "actual_closest_km": actual_closest_km,
            "actual_cpa_at": actual_cpa_at,
            "timing_error_s": round(timing_error_s, 1) if timing_error_s is not None else None,
            "scoreable": scoreable,
            "timing_scoreable": timing_scoreable,
            "outcome_version": (evidence or {}).get("outcome_version") or OUTCOME_VERSION,
            "coverage_missing": not scoreable,
            "coverage_resolution": coverage_resolution,
            "coverage_mode": "historical_flight_number_timing_shadow",
            "missing_coverage_policy": "inconclusive",
            "negative_coverage_proven": bool((evidence or {}).get("negative_coverage_proven")),
            "negative_coverage_sample_count": int((evidence or {}).get("negative_coverage_sample_count") or 0),
            "negative_coverage_max_gap_s": (evidence or {}).get("negative_coverage_max_gap_s"),
            "negative_coverage_min_lower_bound_km": (evidence or {}).get("negative_coverage_min_lower_bound_km"),
            "shadow_only": True,
            "note": "Missing ADS-B coverage is inconclusive and is never scored as a hit or miss.",
        }
        written = await append_evidence(outcome_doc, root=base)
        if written is None:
            continue
        resolved_map[event_id] = current.isoformat().replace("+00:00", "Z")
        changed = True
        counters["resolved"] += 1
        counters["scoreable" if scoreable else "inconclusive"] += 1

    if changed:
        if len(resolved_map) > STATE_MAX_RESOLVED:
            items = list(resolved_map.items())[-STATE_MAX_RESOLVED:]
            resolved_map = dict(items)
        state.update({
            "schema": "plane-alerts-next60-outcome-state-v55",
            "updated_at": current.isoformat().replace("+00:00", "Z"),
            "resolved": resolved_map,
        })
        await asyncio.to_thread(_atomic_state, base, state)
    return counters
