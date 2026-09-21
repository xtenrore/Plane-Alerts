"""Plane Alerts v5.3 multi-location scale helpers.

The shared provider layer can cover many nearby users with one ADS-B snapshot.
This module adds a compact user spatial index so each aircraft is matched only
to observers whose expanded monitoring envelope can contain it. The coarse grid
never decides membership: an exact spherical central-angle threshold remains
authoritative after candidate lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
import math
from typing import Any

from app.config import settings
from app.worker.geo import haversine

CELL_DEG = 0.5
EXTRA_TRACKING_MARGIN_KM = 120.0
MAX_INDEX_CELLS_PER_USER = 256
MAX_CANDIDATE_AIRCRAFT_PER_USER = 4096
EARTH_RADIUS_KM = 6371.0088
_DOT_EPSILON = 1e-12


@dataclass(slots=True)
class IndexedUser:
    user: dict
    user_id: int
    x: float
    y: float
    z: float
    min_dot: float


@dataclass(slots=True)
class SpatialIndex:
    cells: dict[tuple[int, int], list[IndexedUser]]
    fallback_users: list[IndexedUser]
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


def _unit_vector(lat: float, lon: float) -> tuple[float, float, float]:
    phi = math.radians(max(-90.0, min(90.0, lat)))
    lam = math.radians(((lon + 180.0) % 360.0) - 180.0)
    cos_phi = math.cos(phi)
    return cos_phi * math.cos(lam), cos_phi * math.sin(lam), math.sin(phi)


def _tracking_radius_km(user: dict) -> float:
    loc = user["location"]
    return max(0.0, float(loc.get("radius_km", settings.default_radius_km))) + EXTRA_TRACKING_MARGIN_KM


def _indexed_user(user: dict) -> IndexedUser:
    loc = user["location"]
    x, y, z = _unit_vector(float(loc["latitude"]), float(loc["longitude"]))
    angular_radius = min(math.pi, _tracking_radius_km(user) / EARTH_RADIUS_KM)
    return IndexedUser(
        user=user,
        user_id=int(user["user_id"]),
        x=x,
        y=y,
        z=z,
        min_dot=math.cos(angular_radius),
    )


def _user_envelope_cells(user: dict) -> list[tuple[int, int]] | None:
    loc = user["location"]
    lat = float(loc["latitude"])
    lon = float(loc["longitude"])
    radius_km = _tracking_radius_km(user)
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
    cells: dict[tuple[int, int], list[IndexedUser]] = {}
    fallback: list[IndexedUser] = []
    for user in users:
        indexed = _indexed_user(user)
        envelope = _user_envelope_cells(user)
        if envelope is None:
            fallback.append(indexed)
            continue
        for cell in envelope:
            cells.setdefault(cell, []).append(indexed)
    return SpatialIndex(cells=cells, fallback_users=fallback, user_count=len(users))


def _candidate_users(index: SpatialIndex, lat: float, lon: float) -> tuple[list[IndexedUser], list[IndexedUser]]:
    """Return disjoint indexed and fallback candidates for one aircraft point."""
    return index.cells.get(_cell(lat, lon), []), index.fallback_users


def _dot(ax: float, ay: float, az: float, user: IndexedUser) -> float:
    return ax * user.x + ay * user.y + az * user.z


def _aircraft_closeness(ac: Any, user: IndexedUser) -> float:
    ax, ay, az = _unit_vector(float(ac.latitude), float(ac.longitude))
    return _dot(ax, ay, az, user)


def partition_aircraft_by_user(users: list[dict], aircraft: list[Any]) -> tuple[dict[int, list[Any]], ScaleStats]:
    """Return exact nearby candidate aircraft per user with bounded fan-out.

    The cell grid is only an acceleration structure. Exact membership uses the
    same spherical great-circle geometry as Haversine, expressed as a dot-
    product threshold ``cos(distance / earth_radius)``. This avoids repeated
    inverse-trigonometric work without approximating the radius boundary.

    Normal regional feeds are far below the 4,096 per-user safety cap, so the
    common path stores aircraft directly and avoids allocating a distance tuple
    for every candidate pair. If a feed actually exceeds the cap, that user's
    list is promoted once to a bounded nearest-aircraft heap. This preserves the
    cap's nearest-first semantics without taxing ordinary cycles.
    """
    index = build_user_spatial_index(users)
    indexed_by_id = {int(user["user_id"]): _indexed_user(user) for user in users}
    by_user: dict[int, list[Any]] = {uid: [] for uid in indexed_by_id}
    overflow_heaps: dict[int, list[tuple[float, int, Any]]] = {}
    sequence = 0

    for ac in aircraft:
        lat = getattr(ac, "latitude", None)
        lon = getattr(ac, "longitude", None)
        if lat is None or lon is None:
            continue
        ac_lat = float(lat)
        ac_lon = float(lon)
        ax, ay, az = _unit_vector(ac_lat, ac_lon)
        indexed_candidates, fallback_candidates = _candidate_users(index, ac_lat, ac_lon)

        # Indexed and fallback sets are disjoint by construction; no per-point
        # dedup allocation is needed on the hot path.
        for candidate_group in (indexed_candidates, fallback_candidates):
            for indexed_user in candidate_group:
                closeness = _dot(ax, ay, az, indexed_user)
                if closeness + _DOT_EPSILON < indexed_user.min_dot:
                    continue
                uid = indexed_user.user_id
                heap = overflow_heaps.get(uid)
                if heap is not None:
                    sequence += 1
                    item = (closeness, sequence, ac)
                    if closeness > heap[0][0]:
                        heapq.heapreplace(heap, item)
                    continue

                bucket = by_user[uid]
                if len(bucket) < MAX_CANDIDATE_AIRCRAFT_PER_USER:
                    bucket.append(ac)
                    continue

                # Rare overflow path: rank the already-retained candidates once,
                # then maintain only the nearest MAX entries with a min-heap.
                ranked_heap: list[tuple[float, int, Any]] = []
                for existing in bucket:
                    sequence += 1
                    ranked_heap.append((_aircraft_closeness(existing, indexed_user), sequence, existing))
                heapq.heapify(ranked_heap)
                sequence += 1
                item = (closeness, sequence, ac)
                if closeness > ranked_heap[0][0]:
                    heapq.heapreplace(ranked_heap, item)
                overflow_heaps[uid] = ranked_heap
                by_user[uid] = []

    capped_users = len(overflow_heaps)
    result: dict[int, list[Any]] = {}
    max_candidates = 0
    candidate_pairs = 0
    for user in users:
        uid = int(user["user_id"])
        heap = overflow_heaps.get(uid)
        if heap is not None:
            heap.sort(key=lambda item: (item[0], item[1]), reverse=True)
            chosen = [ac for _, _, ac in heap]
        else:
            chosen = by_user.get(uid, [])
        result[uid] = chosen
        candidate_pairs += len(chosen)
        max_candidates = max(max_candidates, len(chosen))

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
