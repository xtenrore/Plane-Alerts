"""Local global airport/runway reference repository for Plane Alerts.

The database is built into the production image from a commit-pinned
OurAirports snapshot. Runtime lookups are local SQLite reads only: no network,
no MongoDB, and no paid service dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
from typing import Iterable

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "aviation" / "compiled" / "global_airports.sqlite3"


@dataclass(slots=True, frozen=True)
class RunwayReference:
    airport_ident: str
    le_ident: str
    le_latitude_deg: float
    le_longitude_deg: float
    le_heading_deg: float
    he_ident: str
    he_latitude_deg: float
    he_longitude_deg: float
    he_heading_deg: float
    closed: bool = False


@dataclass(slots=True, frozen=True)
class AirportReference:
    ident: str
    gps_code: str
    iata_code: str
    local_code: str
    name: str
    airport_type: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float
    closed: bool
    runways: tuple[RunwayReference, ...] = ()


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def _normalise_code(value: str | None) -> str:
    return str(value or "").strip().upper()


def _cell_ranges(latitude: float, longitude: float, radius_km: float) -> tuple[range, tuple[range, ...]]:
    lat_delta = min(90.0, max(0.02, radius_km / 110.574))
    cos_lat = max(0.02, abs(math.cos(math.radians(latitude))))
    lon_delta = min(180.0, max(0.02, radius_km / (111.320 * cos_lat)))
    lat_min = max(-90.0, latitude - lat_delta)
    lat_max = min(90.0, latitude + lat_delta)
    lat_cells = range(max(-90, math.floor(lat_min)), min(89, math.floor(lat_max)) + 1)

    lon = ((longitude + 180.0) % 360.0) - 180.0
    low = lon - lon_delta
    high = lon + lon_delta
    if low < -180:
        lon_ranges = (
            range(math.floor(low + 360), 180),
            range(-180, math.floor(high) + 1),
        )
    elif high >= 180:
        lon_ranges = (
            range(math.floor(low), 180),
            range(-180, math.floor(high - 360) + 1),
        )
    else:
        lon_ranges = (range(math.floor(low), math.floor(high) + 1),)
    return lat_cells, lon_ranges


class AirportRepository:
    def __init__(self, path: Path | str | None = None) -> None:
        configured = os.getenv("PLANE_ALERTS_AIRPORT_DB", "").strip()
        self.path = Path(path or configured or DEFAULT_DB_PATH)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._warned_unavailable = False

    def _connection(self) -> sqlite3.Connection | None:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if not self.path.is_file():
                if not self._warned_unavailable:
                    logger.warning(
                        "global_airport_database_unavailable path=%s terminal_intelligence=metadata_fallback",
                        self.path,
                    )
                    self._warned_unavailable = True
                return None
            try:
                uri = f"file:{self.path.resolve()}?mode=ro&immutable=1"
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=0.05)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                self._conn = conn
                logger.info(
                    "global_airport_database_loaded path=%s airports=%s runways=%s source_commit=%s",
                    self.path,
                    self.metadata("airport_count"),
                    self.metadata("runway_count"),
                    self.metadata("source_commit"),
                )
                return conn
            except sqlite3.Error:
                logger.exception("global_airport_database_open_failed path=%s", self.path)
                return None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        self._airport_by_code_cached.cache_clear()
        self._runways_cached.cache_clear()
        self._cell_candidates_cached.cache_clear()

    def metadata(self, key: str) -> str:
        conn = self._conn or self._connection()
        if conn is None:
            return ""
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else ""
        except sqlite3.Error:
            return ""

    @property
    def available(self) -> bool:
        return self._connection() is not None

    @lru_cache(maxsize=4096)
    def _runways_cached(self, ident: str) -> tuple[RunwayReference, ...]:
        conn = self._connection()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                """
                SELECT airport_ident, le_ident, le_latitude_deg, le_longitude_deg, le_heading_deg,
                       he_ident, he_latitude_deg, he_longitude_deg, he_heading_deg, closed
                FROM runways
                WHERE airport_ident = ? AND closed = 0
                ORDER BY id
                """,
                (ident,),
            ).fetchall()
        except sqlite3.Error:
            logger.exception("global_airport_runway_lookup_failed ident=%s", ident)
            return ()
        out: list[RunwayReference] = []
        for row in rows:
            required = (
                row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8]
            )
            if any(value in (None, "") for value in required):
                continue
            try:
                out.append(
                    RunwayReference(
                        airport_ident=str(row[0]),
                        le_ident=str(row[1]),
                        le_latitude_deg=float(row[2]),
                        le_longitude_deg=float(row[3]),
                        le_heading_deg=float(row[4]) % 360.0,
                        he_ident=str(row[5]),
                        he_latitude_deg=float(row[6]),
                        he_longitude_deg=float(row[7]),
                        he_heading_deg=float(row[8]) % 360.0,
                        closed=bool(row[9]),
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(out)

    def _to_reference(self, row: sqlite3.Row | None, *, with_runways: bool = True) -> AirportReference | None:
        if row is None:
            return None
        ident = str(row["ident"])
        return AirportReference(
            ident=ident,
            gps_code=str(row["gps_code"] or ""),
            iata_code=str(row["iata_code"] or ""),
            local_code=str(row["local_code"] or ""),
            name=str(row["name"] or ident),
            airport_type=str(row["airport_type"] or "unknown"),
            latitude_deg=float(row["latitude_deg"]),
            longitude_deg=float(row["longitude_deg"]),
            elevation_m=float(row["elevation_m"] or 0.0),
            closed=bool(row["closed"]),
            runways=self._runways_cached(ident) if with_runways else (),
        )

    @lru_cache(maxsize=8192)
    def _airport_by_code_cached(self, code: str, include_closed: bool) -> AirportReference | None:
        conn = self._connection()
        if conn is None or not code:
            return None
        closed_clause = "" if include_closed else " AND closed = 0"
        query = (
            "SELECT * FROM airports WHERE "
            "(ident = ? OR gps_code = ? OR iata_code = ? OR local_code = ?)"
            + closed_clause
            + " ORDER BY CASE WHEN ident = ? THEN 0 WHEN gps_code = ? THEN 1 WHEN iata_code = ? THEN 2 ELSE 3 END LIMIT 1"
        )
        try:
            row = conn.execute(query, (code, code, code, code, code, code, code)).fetchone()
            return self._to_reference(row)
        except sqlite3.Error:
            logger.exception("global_airport_code_lookup_failed code=%s", code)
            return None

    def by_code(self, code: str | None, *, include_closed: bool = False) -> AirportReference | None:
        return self._airport_by_code_cached(_normalise_code(code), include_closed)

    @lru_cache(maxsize=4096)
    def _cell_candidates_cached(self, lat_cell: int, lon_cell: int) -> tuple[str, ...]:
        conn = self._connection()
        if conn is None:
            return ()
        try:
            rows = conn.execute(
                "SELECT airport_ident FROM airport_cells WHERE cell_lat = ? AND cell_lon = ?",
                (lat_cell, lon_cell),
            ).fetchall()
            return tuple(str(row[0]) for row in rows)
        except sqlite3.Error:
            logger.exception("global_airport_cell_lookup_failed cell=%s,%s", lat_cell, lon_cell)
            return ()

    def nearby(
        self,
        latitude: float,
        longitude: float,
        *,
        max_distance_km: float = 120.0,
        limit: int = 12,
        include_closed: bool = False,
    ) -> tuple[AirportReference, ...]:
        conn = self._connection()
        if conn is None:
            return ()
        try:
            latitude = float(latitude)
            longitude = float(longitude)
            radius = max(0.1, min(1000.0, float(max_distance_km)))
        except (TypeError, ValueError):
            return ()
        if not math.isfinite(latitude) or not math.isfinite(longitude) or not (-90 <= latitude <= 90):
            return ()

        lat_cells, lon_ranges = _cell_ranges(latitude, longitude, radius)
        candidate_ids: set[str] = set()
        for lat_cell in lat_cells:
            for lon_range in lon_ranges:
                for lon_cell in lon_range:
                    candidate_ids.update(self._cell_candidates_cached(lat_cell, lon_cell))
        if not candidate_ids:
            return ()

        placeholders = ",".join("?" for _ in candidate_ids)
        query = f"SELECT * FROM airports WHERE ident IN ({placeholders})"
        if not include_closed:
            query += " AND closed = 0"
        try:
            rows = conn.execute(query, tuple(candidate_ids)).fetchall()
        except sqlite3.Error:
            logger.exception("global_airport_nearby_lookup_failed")
            return ()

        ranked: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            distance = _haversine_km(latitude, longitude, float(row["latitude_deg"]), float(row["longitude_deg"]))
            if distance <= radius:
                ranked.append((distance, row))
        ranked.sort(key=lambda item: (item[0], str(item[1]["ident"])))
        return tuple(
            ref
            for _, row in ranked[: max(1, min(64, int(limit)))]
            if (ref := self._to_reference(row)) is not None
        )

    def nearest(
        self,
        latitude: float,
        longitude: float,
        *,
        max_distance_km: float = 120.0,
        include_closed: bool = False,
    ) -> AirportReference | None:
        rows = self.nearby(
            latitude,
            longitude,
            max_distance_km=max_distance_km,
            limit=1,
            include_closed=include_closed,
        )
        return rows[0] if rows else None

    def counts(self) -> tuple[int, int]:
        conn = self._connection()
        if conn is None:
            return 0, 0
        try:
            airports = int(conn.execute("SELECT COUNT(*) FROM airports").fetchone()[0])
            runways = int(conn.execute("SELECT COUNT(*) FROM runways").fetchone()[0])
            return airports, runways
        except sqlite3.Error:
            return 0, 0


airport_repository = AirportRepository()
