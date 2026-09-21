"""Plane Alerts v5.3 multi-location scale helpers.

The shared provider layer can cover many nearby users with one ADS-B snapshot.
This module adds a compact user spatial index so each aircraft is matched only
to observers whose expanded monitoring envelope can contain it. Exact distance
checks remain authoritative after the coarse cell lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.config import settings
from app.worker.geo import haversine

CELL_DEG = 0.5
EXTRA_TRACKING_MARGIN_KM = 120.0
MAX_INDEX_CELLS_PER_USER = 256
MAX_CANDIDATE_AIRCRAFT_PER_USER = 4096


@dataclass(slots=True)
class SpatialIndex:
    cells: dict[tuple[int, int], list[dict]]
    fallback_users: list[dict]
    user_count: int


@dataclass(slots=True)
class ScaleStats:
    users: int
    aircraft: int
    candidate_pairs: int
    full_pairs: int
    indexed_cells: int
    fallback_users: int
    max_candidates_per_user: int
    capped_users: int

    def as_dict(self) -> dict[str, int | float]:
        reduction = 0.0
        if self.full_pairs:
            reduction = 1.0 - self.candidate_pairs / self.full_pairs
        return {
            "users": self.users,
            "aircraft": self.aircraft,
            "candidate_pairs": self.candidate_pairs,
            "full_pairs": self.full_pairs,
            "indexed_cells": self.indexed_cells,
            "fallback_users": self.fallback_users,
            "max_candidates_per_user": self.max_candidates_per_user,
            "capped_users": self.capped_users,
            "pair_reduction_ratio": round(max(0.0, min(1.0, reduction)), 6),
        }


def _cell(lat: float, lon: float) -> tuple[int, int]:
    lat_i = math.floor((max(-90.0, min(90.0, lat)) + 90.0) / CELL_DEG)
    wrapped_lon = ((lon + 180.0) % 360.0) - 180.0
    lon_i = math.floor((wrapped_lon + 180.0) / CELL_DEG)
    return lat_i, lon_i


def _user_envelope_cells(user: dict) -> list[tuple[int, int]] | None:
    loc = user["location"]
    lat = float(loc["latitude"])
    lon = float(loc["longitude"])
    radius_km = max(0.0, float(loc.get("radius_km", settings.default_radius_km))) + EXTRA_TRACKING_MARGIN_KM
    lat_delta = min(90.0, radius_km / 111.32)
    cos_lat = max(0.05, abs(math.cos(math.radians(lat))))
    lon_delta = min(180.0, radius_km / (111.32 * cos_lat))

    min_lat = max(-90.0, lat - lat_delta)
    max_lat = min(90.0, lat + lat_delta)
    min_lon = lon - lon_delta
    max_lon = lon + lon_delta

    lat_start, _ = _cell(min_lat, lon)
    lat_end, _ = _cell(max_lat, lon)

    lon_ranges: list[tuple[float, float]]
    if min_lon < -180.0:
        lon_ranges = [(min_lon + 360.0, 180.0), (-180.0, max_lon)]
    elif max_lon >= 180.0:
        lon_ranges = [(min_lon, 180.0 - 1e-9), (-180.0, max_lon - 360.0)]
    else:
        lon_ranges = [(min_lon, max_lon)]

    cells: list[tuple[int, int]] = []
    for range_min, range_max in lon_ranges:
        _, lon_start = _cell(lat, range_min)
        _, lon_end = _cell(lat, range_max)
        for lat_i in range(lat_start, lat_end + 1):
            for lon_i in range(lon_start, lon_end + 1):
                cells.append((lat_i, lon_i))
                if len(cells) > MAX_INDEX_CELLS_PER_USER:
                    return None
    return cells


def build_user_spatial_index(users: list[dict]) -> SpatialIndex:
    cells: dict[tuple[int, int], list[dict]] = {}
    fallback: list[dict] = []
    for user in users:
        envelope = _user_envelope_cells(user)
        if envelope is None:
            fallback.append(user)
            continue
        for cell in envelope:
            cells.setdefault(cell, []).append(user)
    return SpatialIndex(cells=cells, fallback_users=fallback, user_count=len(users))


def _candidate_users(index: SpatialIndex, aircraft: Any) -> list[dict]:
    lat = getattr(aircraft, "latitude", None)
    lon = getattr(aircraft, "longitude", None)
    if lat is None or lon is None:
        return []
    candidates = list(index.cells.get(_cell(float(lat), float(lon)), ()))
    if index.fallback_users:
        candidates.extend(index.fallback_users)
    unique: dict[int, dict] = {}
    for user in candidates:
        unique[int(user["user_id"])] = user
    return list(unique.values())


def partition_aircraft_by_user(users: list[dict], aircraft: list[Any]) -> tuple[dict[int, list[Any]], ScaleStats]:
    """Return exact nearby candidate aircraft per user with bounded fan-out.

    The cell grid is only an acceleration structure. Every candidate is checked
    with Haversine distance against the same ``radius + 120 km`` envelope used
    by the existing monitor, so cell boundaries cannot create a false match.
    If an unusually large feed exceeds the per-user safety cap, the nearest
    aircraft are retained; the cap is intentionally far above normal regional
    public-feed counts and is exposed in stats for operator visibility.
    """
    index = build_user_spatial_index(users)
    by_user: dict[int, list[tuple[float, Any]]] = {int(user["user_id"]): [] for user in users}

    for ac in aircraft:
        lat = getattr(ac, "latitude", None)
        lon = getattr(ac, "longitude", None)
        if lat is None or lon is None:
            continue
        ac_lat = float(lat)
        ac_lon = float(lon)
        for user in _candidate_users(index, ac):
            loc = user["location"]
            radius_km = max(0.0, float(loc.get("radius_km", settings.default_radius_km))) + EXTRA_TRACKING_MARGIN_KM
            distance = haversine(float(loc["latitude"]), float(loc["longitude"]), ac_lat, ac_lon)
            if distance <= radius_km:
                by_user[int(user["user_id"])].append((distance, ac))

    capped_users = 0
    result: dict[int, list[Any]] = {}
    max_candidates = 0
    candidate_pairs = 0
    for user in users:
        uid = int(user["user_id"])
        ranked = by_user.get(uid, [])
        if len(ranked) > MAX_CANDIDATE_AIRCRAFT_PER_USER:
            ranked.sort(key=lambda pair: pair[0])
            ranked = ranked[:MAX_CANDIDATE_AIRCRAFT_PER_USER]
            capped_users += 1
        result[uid] = [ac for _, ac in ranked]
        candidate_pairs += len(ranked)
        max_candidates = max(max_candidates, len(ranked))

    stats = ScaleStats(
        users=len(users),
        aircraft=len(aircraft),
        candidate_pairs=candidate_pairs,
        full_pairs=len(users) * len(aircraft),
        indexed_cells=len(index.cells),
        fallback_users=len(index.fallback_users),
        max_candidates_per_user=max_candidates,
        capped_users=capped_users,
    )
    return result, stats
