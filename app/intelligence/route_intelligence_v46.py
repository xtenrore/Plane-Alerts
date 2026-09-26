"""Plane Alerts v4.6 decayed route evidence and bounded route clustering.

Route history is built in the existing background refresh path. Live geometry
remains authoritative: history and destination context are supporting analytics
and must not become a second live route-guard authority.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Iterable

from app.config import settings
from app.database import get_db
from app.intelligence import route_history as route_mod
from app.intelligence.trajectory import haversine_km

_HISTORY_LOOKBACK_DAYS = 28
_HISTORY_RETENTION_DAYS = 35
_HISTORY_MAX_PATHS = 28
_CLUSTER_COMPARE_POINTS = 24
_CLUSTER_THRESHOLD_KM = 10.0
_HISTORY_HALF_LIFE_DAYS = 7.0

_INSTALLED = False


class RouteHistoryBundle(list):
    """List-compatible history with precomputed background cluster metadata."""

    def __init__(self, paths: Iterable[list[route_mod.RoutePoint]], *, age_days: list[float], clusters: list[int]) -> None:
        super().__init__(paths)
        self.age_days = list(age_days)
        self.clusters = list(clusters)


@dataclass(slots=True, frozen=True)
class RouteClusterSummary:
    cluster_id: int
    sample_count: int
    decayed_weight: float
    newest_age_days: float
    representative_index: int


def _decay_weight(age_days: float) -> float:
    return 0.5 ** (max(0.0, float(age_days)) / _HISTORY_HALF_LIFE_DAYS)


def _reduce_path(path: Iterable[Any], max_points: int = _CLUSTER_COMPARE_POINTS) -> list[route_mod.RoutePoint]:
    points = route_mod._clean_points(path, max_points=max_points)
    return points


def _cluster_paths(paths: list[list[route_mod.RoutePoint]]) -> list[int]:
    """Greedy deterministic route clustering, executed only in background refresh."""
    if not paths:
        return []
    representatives: list[list[route_mod.RoutePoint]] = []
    labels: list[int] = []
    for path in paths:
        reduced = _reduce_path(path)
        best_index: int | None = None
        best_distance = math.inf
        for index, representative in enumerate(representatives):
            distance = route_mod.route_similarity_km(reduced, representative)
            if distance is not None and distance < best_distance:
                best_index, best_distance = index, distance
        if best_index is not None and best_distance <= _CLUSTER_THRESHOLD_KM:
            labels.append(best_index)
        else:
            representatives.append(reduced)
            labels.append(len(representatives) - 1)
    return labels


def cluster_summaries(bundle: RouteHistoryBundle) -> list[RouteClusterSummary]:
    groups: dict[int, list[int]] = {}
    for index, cluster_id in enumerate(bundle.clusters):
        groups.setdefault(int(cluster_id), []).append(index)
    out: list[RouteClusterSummary] = []
    for cluster_id in sorted(groups):
        indexes = groups[cluster_id]
        weights = [_decay_weight(bundle.age_days[index]) for index in indexes]
        representative = min(indexes, key=lambda index: bundle.age_days[index])
        out.append(RouteClusterSummary(
            cluster_id=cluster_id,
            sample_count=len(indexes),
            decayed_weight=sum(weights),
            newest_age_days=min(bundle.age_days[index] for index in indexes),
            representative_index=representative,
        ))
    return out


async def observe_v46(self: route_mod.RouteHistoryService, ac: Any, *, now: float | None = None) -> None:
    """Persist bounded route samples with enough retention for meaningful decay."""
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    if not key or getattr(ac, "latitude", None) is None or getattr(ac, "longitude", None) is None:
        return
    now_ts = time.time() if now is None else float(now)
    lat, lon = float(ac.latitude), float(ac.longitude)
    previous = self._last_sample.get(key)
    interval = max(15, int(settings.route_sample_interval_seconds))
    if previous:
        last_t, last_lat, last_lon = previous
        moved = haversine_km(last_lat, last_lon, lat, lon)
        if now_ts - last_t < interval and moved < 1.5:
            return
    self._last_sample[key] = (now_ts, lat, lon)
    day = datetime.fromtimestamp(now_ts, timezone.utc).date().isoformat()
    captured = datetime.now(timezone.utc)
    point = {
        "t": round(now_ts, 1),
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        "altitude_m": getattr(ac, "altitude", None),
        "heading_deg": getattr(ac, "heading", None),
    }
    await get_db()["flight_route_samples"].update_one(
        {"callsign": key, "utc_date": day},
        {
            "$set": {
                "callsign": key,
                "utc_date": day,
                "updated_at": captured,
                "expires_at": captured + timedelta(days=_HISTORY_RETENTION_DAYS),
            },
            "$push": {"points": {"$each": [point], "$slice": -360}},
        },
        upsert=True,
    )


async def historical_paths_v46(
    self: route_mod.RouteHistoryService,
    key: str,
    *,
    now: datetime | None = None,
) -> RouteHistoryBundle:
    """Load and cluster bounded history for analytics and shadow evidence."""
    current = now or datetime.now(timezone.utc)
    wanted = [
        (current.date() - timedelta(days=offset)).isoformat()
        for offset in range(1, _HISTORY_LOOKBACK_DAYS + 1)
    ]
    cursor = (
        get_db()["flight_route_samples"]
        .find({"callsign": key, "utc_date": {"$in": wanted}}, {"points": 1, "utc_date": 1, "_id": 0})
        .sort("utc_date", -1)
        .limit(_HISTORY_MAX_PATHS)
    )
    docs = [doc async for doc in cursor]
    paths: list[list[route_mod.RoutePoint]] = []
    ages: list[float] = []
    for doc in docs:
        points = route_mod._clean_points(doc.get("points") or [])
        if len(points) < 3:
            continue
        try:
            route_date = datetime.fromisoformat(str(doc.get("utc_date"))).date()
            age = max(1.0, float((current.date() - route_date).days))
        except (TypeError, ValueError):
            age = float(_HISTORY_LOOKBACK_DAYS)
        paths.append(points)
        ages.append(age)
    return RouteHistoryBundle(paths, age_days=ages, clusters=_cluster_paths(paths))


def _future_cpa(
    path: list[route_mod.RoutePoint],
    *,
    aircraft_lat: float,
    aircraft_lon: float,
    observer_lat: float,
    observer_lon: float,
) -> float | None:
    if not path:
        return None
    nearest = min(
        range(len(path)),
        key=lambda index: haversine_km(aircraft_lat, aircraft_lon, path[index].latitude, path[index].longitude),
    )
    future = path[nearest:]
    if not future:
        return None
    return min(haversine_km(point.latitude, point.longitude, observer_lat, observer_lon) for point in future)


def _weighted_median(values: list[tuple[float, float]]) -> float | None:
    usable = sorted((value, max(0.0, weight)) for value, weight in values if math.isfinite(value) and weight > 0)
    if not usable:
        return None
    total = sum(weight for _, weight in usable)
    cumulative = 0.0
    for value, weight in usable:
        cumulative += weight
        if cumulative >= total * 0.5:
            return value
    return usable[-1][0]


def history_features_v46(
    current_path: Iterable[Any],
    historical_paths: Iterable[Iterable[Any]],
    *,
    observer_lat: float,
    observer_lon: float,
    radius_km: float,
    aircraft_lat: float,
    aircraft_lon: float,
) -> tuple[float, str, float | None]:
    """Return decayed, cluster-aware supporting route evidence."""
    current = route_mod._clean_points(current_path)
    if len(current) < 2:
        return 0.0, "none", None

    raw = historical_paths
    paths = [route_mod._clean_points(path) for path in raw]
    paths = [path for path in paths if len(path) >= 3]
    if not paths:
        return 0.0, "none", None

    if isinstance(raw, RouteHistoryBundle) and len(raw.age_days) >= len(paths):
        ages = list(raw.age_days[: len(paths)])
        clusters = list(raw.clusters[: len(paths)])
    else:
        ages = [1.0 + index for index in range(len(paths))]
        clusters = list(range(len(paths)))

    scale = max(5.0, radius_km * 0.35)
    by_cluster: dict[int, list[tuple[float, float, float | None]]] = {}
    all_future: list[tuple[float, float]] = []
    for index, path in enumerate(paths):
        similarity = route_mod._current_to_history_similarity_km(current, path)
        if similarity is None:
            continue
        age_weight = _decay_weight(ages[index])
        closeness = 1.0 - max(0.0, min(1.0, similarity / (scale * 1.8)))
        evidence_weight = age_weight * max(0.05, closeness)
        future_cpa = _future_cpa(
            path,
            aircraft_lat=aircraft_lat,
            aircraft_lon=aircraft_lon,
            observer_lat=observer_lat,
            observer_lon=observer_lon,
        )
        by_cluster.setdefault(int(clusters[index]), []).append((similarity, evidence_weight, future_cpa))
        if future_cpa is not None:
            all_future.append((future_cpa, evidence_weight))

    if not by_cluster:
        return 0.0, "none", None

    cluster_rows: list[tuple[int, float, float, list[float]]] = []
    for cluster_id, rows in by_cluster.items():
        similarities = [row[0] for row in rows]
        weight = sum(row[1] for row in rows)
        future = [row[2] for row in rows if row[2] is not None]
        cluster_rows.append((cluster_id, median(similarities), weight, future))

    best = min(cluster_rows, key=lambda row: row[1])
    best_similarity = best[1]
    match_score = 1.0 - max(0.0, min(1.0, best_similarity / (scale * 1.8)))
    total_decayed = sum(row[2] for row in cluster_rows) or 1.0
    dominance = best[2] / total_decayed

    sample_count = len(paths)
    if sample_count < 3:
        match_score *= 0.60
        label = "small-sample"
    elif len(cluster_rows) == 1:
        label = "single-route-cluster"
    else:
        outcomes: set[bool] = set()
        for _, _, weight, future in cluster_rows:
            if weight < total_decayed * 0.18 or not future:
                continue
            outcomes.add(median(future) <= radius_km)
        if len(outcomes) > 1:
            match_score *= 0.65
            label = "contradictory-clusters"
        else:
            match_score *= max(0.70, min(1.0, dominance + 0.20))
            label = "multiple-clusters"

    freshness_weight = min(1.0, total_decayed / max(1.0, min(4.0, sample_count)))
    match_score *= max(0.35, freshness_weight)
    history_cpa = _weighted_median(all_future)
    return max(0.0, min(1.0, match_score)), label, history_cpa


def neutralize_hard_route_veto_v46(result: route_mod.RouteGateResult) -> route_mod.RouteGateResult:
    """Compatibility helper retained for historical analytics/replay code only."""
    reason = str(result.reason or "").lower()
    unsafe_history = (
        "today's route diverges" in reason
        or "last three flight-number routes are inconsistent" in reason
    )
    destination_only = reason.startswith("known destination ")
    if result.suppress_alert and (unsafe_history or destination_only):
        return replace(
            result,
            suppress_alert=False,
            expected_turn_pending=bool(destination_only),
            reason=(
                "terminal destination/history is supporting uncertainty evidence; live geometry remains authoritative"
                if destination_only
                else "historical routes are inconsistent/diverged; live trajectory regains authority"
            ),
        )
    return result


def install_route_intelligence_v46() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Route clustering/history remains available to Prediction Lab, Next 60 and
    # diagnostics. It no longer patches any historical live route-guard layer.
    route_mod.RouteHistoryService._historical_paths = historical_paths_v46
    _INSTALLED = True
