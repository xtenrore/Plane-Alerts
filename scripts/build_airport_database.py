#!/usr/bin/env python3
"""Build Plane Alerts' local read-only global airport/runway database.

The production image uses a commit-pinned OurAirports snapshot. Runtime code
never needs an OurAirports API key or network access. Official/maintained Plane
Alerts overrides are applied after the global import.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "data" / "aviation" / "ourairports" / "source_manifest.json"
DEFAULT_OVERRIDES = ROOT / "data" / "aviation" / "overrides"
DEFAULT_OUTPUT = ROOT / "data" / "aviation" / "compiled" / "global_airports.sqlite3"
USER_AGENT = "Plane-Alerts-airport-data-builder/4.7.1"

AIRPORT_FIELDS = {
    "id", "ident", "type", "name", "latitude_deg", "longitude_deg",
    "elevation_ft", "continent", "iso_country", "iso_region", "municipality",
    "scheduled_service", "gps_code", "iata_code", "local_code",
}
RUNWAY_FIELDS = {
    "id", "airport_ident", "length_ft", "width_ft", "surface", "lighted", "closed",
    "le_ident", "le_latitude_deg", "le_longitude_deg", "le_elevation_ft", "le_heading_degT",
    "he_ident", "he_latitude_deg", "he_longitude_deg", "he_elevation_ft", "he_heading_degT",
}


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _metres_from_ft(value: Any) -> float | None:
    number = _float(value)
    return None if number is None else number * 0.3048


def _cell(latitude: float, longitude: float) -> tuple[int, int]:
    # One-degree coarse cells keep the on-disk index tiny. Exact haversine
    # filtering happens after candidate retrieval in the runtime repository.
    lat_cell = max(-90, min(89, math.floor(latitude)))
    lon = ((longitude + 180.0) % 360.0) - 180.0
    lon_cell = max(-180, min(179, math.floor(lon)))
    return lat_cell, lon_cell


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def _assert_headers(reader: csv.DictReader, required: set[str], label: str) -> None:
    fields = set(reader.fieldnames or ())
    missing = sorted(required - fields)
    if missing:
        raise RuntimeError(f"{label} schema missing required columns: {missing}")


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA foreign_keys=OFF;

        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE airports (
            ident TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            airport_type TEXT NOT NULL,
            name TEXT NOT NULL,
            latitude_deg REAL NOT NULL,
            longitude_deg REAL NOT NULL,
            elevation_m REAL,
            continent TEXT,
            iso_country TEXT,
            iso_region TEXT,
            municipality TEXT,
            scheduled_service INTEGER NOT NULL DEFAULT 0,
            gps_code TEXT,
            iata_code TEXT,
            local_code TEXT,
            closed INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE runways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT,
            airport_ident TEXT NOT NULL,
            length_m REAL,
            width_m REAL,
            surface TEXT,
            lighted INTEGER NOT NULL DEFAULT 0,
            closed INTEGER NOT NULL DEFAULT 0,
            le_ident TEXT,
            le_latitude_deg REAL,
            le_longitude_deg REAL,
            le_elevation_m REAL,
            le_heading_deg REAL,
            he_ident TEXT,
            he_latitude_deg REAL,
            he_longitude_deg REAL,
            he_elevation_m REAL,
            he_heading_deg REAL,
            source TEXT NOT NULL
        );

        CREATE TABLE airport_cells (
            cell_lat INTEGER NOT NULL,
            cell_lon INTEGER NOT NULL,
            airport_ident TEXT NOT NULL,
            PRIMARY KEY (cell_lat, cell_lon, airport_ident)
        ) WITHOUT ROWID;

        CREATE INDEX idx_airports_gps ON airports(gps_code);
        CREATE INDEX idx_airports_iata ON airports(iata_code);
        CREATE INDEX idx_airports_local ON airports(local_code);
        CREATE INDEX idx_runways_airport ON runways(airport_ident);
        CREATE INDEX idx_cells_airport ON airport_cells(airport_ident);
        """
    )


def _insert_airports(conn: sqlite3.Connection, path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _assert_headers(reader, AIRPORT_FIELDS, "airports.csv")
        for row in reader:
            ident = str(row.get("ident") or "").strip().upper()
            lat = _float(row.get("latitude_deg"))
            lon = _float(row.get("longitude_deg"))
            if not ident or lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                continue
            airport_type = str(row.get("type") or "unknown").strip()
            conn.execute(
                """
                INSERT OR REPLACE INTO airports (
                    ident, source_id, airport_type, name, latitude_deg, longitude_deg,
                    elevation_m, continent, iso_country, iso_region, municipality,
                    scheduled_service, gps_code, iata_code, local_code, closed, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ourairports')
                """,
                (
                    ident,
                    str(row.get("id") or ""),
                    airport_type,
                    str(row.get("name") or ident).strip(),
                    lat,
                    lon,
                    _metres_from_ft(row.get("elevation_ft")),
                    str(row.get("continent") or "").strip(),
                    str(row.get("iso_country") or "").strip().upper(),
                    str(row.get("iso_region") or "").strip().upper(),
                    str(row.get("municipality") or "").strip(),
                    1 if str(row.get("scheduled_service") or "").lower() == "yes" else 0,
                    str(row.get("gps_code") or "").strip().upper() or None,
                    str(row.get("iata_code") or "").strip().upper() or None,
                    str(row.get("local_code") or "").strip().upper() or None,
                    1 if airport_type == "closed" else 0,
                ),
            )
            cell_lat, cell_lon = _cell(lat, lon)
            conn.execute(
                "INSERT OR REPLACE INTO airport_cells(cell_lat, cell_lon, airport_ident) VALUES (?, ?, ?)",
                (cell_lat, cell_lon, ident),
            )
            count += 1
    return count


def _insert_runways(conn: sqlite3.Connection, path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _assert_headers(reader, RUNWAY_FIELDS, "runways.csv")
        for row in reader:
            ident = str(row.get("airport_ident") or "").strip().upper()
            if not ident:
                continue
            # Keep every runway row from the snapshot, even when endpoint
            # coordinates/headings are unavailable. Runtime geometry simply
            # ignores incomplete ends rather than inventing precision.
            conn.execute(
                """
                INSERT INTO runways (
                    source_id, airport_ident, length_m, width_m, surface, lighted, closed,
                    le_ident, le_latitude_deg, le_longitude_deg, le_elevation_m, le_heading_deg,
                    he_ident, he_latitude_deg, he_longitude_deg, he_elevation_m, he_heading_deg,
                    source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ourairports')
                """,
                (
                    str(row.get("id") or ""),
                    ident,
                    _metres_from_ft(row.get("length_ft")),
                    _metres_from_ft(row.get("width_ft")),
                    str(row.get("surface") or "").strip() or None,
                    _int(row.get("lighted")),
                    _int(row.get("closed")),
                    str(row.get("le_ident") or "").strip().upper() or None,
                    _float(row.get("le_latitude_deg")),
                    _float(row.get("le_longitude_deg")),
                    _metres_from_ft(row.get("le_elevation_ft")),
                    _float(row.get("le_heading_degT")),
                    str(row.get("he_ident") or "").strip().upper() or None,
                    _float(row.get("he_latitude_deg")),
                    _float(row.get("he_longitude_deg")),
                    _metres_from_ft(row.get("he_elevation_ft")),
                    _float(row.get("he_heading_degT")),
                ),
            )
            count += 1
    return count


def _apply_override(conn: sqlite3.Connection, payload: dict[str, Any], filename: str) -> None:
    ident = str(payload.get("ident") or "").strip().upper()
    if not ident:
        raise RuntimeError(f"override {filename} has no ident")
    existing = conn.execute("SELECT * FROM airports WHERE ident = ?", (ident,)).fetchone()
    lat = _float(payload.get("latitude_deg"))
    lon = _float(payload.get("longitude_deg"))
    if existing is None and (lat is None or lon is None):
        raise RuntimeError(f"override {filename} cannot create {ident} without coordinates")

    if existing is not None:
        lat = existing[4] if lat is None else lat
        lon = existing[5] if lon is None else lon
        values = {
            "source_id": existing[1],
            "airport_type": existing[2],
            "name": existing[3],
            "elevation_m": existing[6],
            "continent": existing[7],
            "iso_country": existing[8],
            "iso_region": existing[9],
            "municipality": existing[10],
            "scheduled_service": existing[11],
            "gps_code": existing[12],
            "iata_code": existing[13],
            "local_code": existing[14],
            "closed": existing[15],
        }
    else:
        values = {
            "source_id": "override",
            "airport_type": "large_airport",
            "name": ident,
            "elevation_m": None,
            "continent": "",
            "iso_country": "",
            "iso_region": "",
            "municipality": "",
            "scheduled_service": 0,
            "gps_code": ident if len(ident) == 4 else None,
            "iata_code": None,
            "local_code": None,
            "closed": 0,
        }

    if payload.get("name"):
        values["name"] = str(payload["name"])
    if payload.get("iata"):
        values["iata_code"] = str(payload["iata"]).strip().upper()
    if payload.get("elevation_m") is not None:
        values["elevation_m"] = _float(payload.get("elevation_m"))

    conn.execute("DELETE FROM airport_cells WHERE airport_ident = ?", (ident,))
    conn.execute(
        """
        INSERT OR REPLACE INTO airports (
            ident, source_id, airport_type, name, latitude_deg, longitude_deg,
            elevation_m, continent, iso_country, iso_region, municipality,
            scheduled_service, gps_code, iata_code, local_code, closed, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ident, values["source_id"], values["airport_type"], values["name"], lat, lon,
            values["elevation_m"], values["continent"], values["iso_country"], values["iso_region"],
            values["municipality"], values["scheduled_service"], values["gps_code"],
            values["iata_code"], values["local_code"], values["closed"],
            f"override:{filename}",
        ),
    )
    cell_lat, cell_lon = _cell(float(lat), float(lon))
    conn.execute(
        "INSERT OR REPLACE INTO airport_cells(cell_lat, cell_lon, airport_ident) VALUES (?, ?, ?)",
        (cell_lat, cell_lon, ident),
    )

    if payload.get("replace_runways"):
        conn.execute("DELETE FROM runways WHERE airport_ident = ?", (ident,))
    for runway in payload.get("runways") or ():
        conn.execute(
            """
            INSERT INTO runways (
                source_id, airport_ident, length_m, width_m, surface, lighted, closed,
                le_ident, le_latitude_deg, le_longitude_deg, le_elevation_m, le_heading_deg,
                he_ident, he_latitude_deg, he_longitude_deg, he_elevation_m, he_heading_deg,
                source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "override", ident, _float(runway.get("length_m")), _float(runway.get("width_m")),
                runway.get("surface"), _int(runway.get("lighted")), _int(runway.get("closed")),
                str(runway.get("le_ident") or "").strip().upper() or None,
                _float(runway.get("le_latitude_deg")), _float(runway.get("le_longitude_deg")),
                _float(runway.get("le_elevation_m")), _float(runway.get("le_heading_deg")),
                str(runway.get("he_ident") or "").strip().upper() or None,
                _float(runway.get("he_latitude_deg")), _float(runway.get("he_longitude_deg")),
                _float(runway.get("he_elevation_m")), _float(runway.get("he_heading_deg")),
                f"override:{filename}",
            ),
        )


def _apply_overrides(conn: sqlite3.Connection, directory: Path) -> int:
    if not directory.exists():
        return 0
    count = 0
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        _apply_override(conn, payload, path.name)
        count += 1
    return count


def build_database(
    airports_csv: Path,
    runways_csv: Path,
    output: Path,
    *,
    manifest: dict[str, Any] | None = None,
    overrides_dir: Path | None = DEFAULT_OVERRIDES,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    conn = sqlite3.connect(temporary)
    try:
        conn.row_factory = sqlite3.Row
        _create_schema(conn)
        airport_count = _insert_airports(conn, airports_csv)
        runway_count = _insert_runways(conn, runways_csv)
        override_count = _apply_overrides(conn, overrides_dir) if overrides_dir else 0

        minimums = (manifest or {}).get("minimum_counts") or {}
        min_airports = int(minimums.get("airports") or 1)
        min_runways = int(minimums.get("runways") or 0)
        if airport_count < min_airports:
            raise RuntimeError(f"airport snapshot incomplete: {airport_count} < {min_airports}")
        if runway_count < min_runways:
            raise RuntimeError(f"runway snapshot incomplete: {runway_count} < {min_runways}")

        source = manifest or {}
        metadata = {
            "schema_version": "1",
            "source": str(source.get("source") or "fixture"),
            "source_commit": str(source.get("commit") or "fixture"),
            "snapshot_utc": str(source.get("snapshot_utc") or "fixture"),
            "airport_count": str(airport_count),
            "runway_count": str(runway_count),
            "override_count": str(override_count),
        }
        conn.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", metadata.items())
        conn.commit()
        conn.execute("ANALYZE")
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()

    os.replace(temporary, output)
    return {
        "airports": airport_count,
        "runways": runway_count,
        "overrides": override_count,
        "bytes": output.stat().st_size,
    }


def _resolve_sources(manifest_path: Path, airports_csv: Path | None, runways_csv: Path | None) -> tuple[dict[str, Any], Path, Path, tempfile.TemporaryDirectory[str] | None]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if airports_csv is not None and runways_csv is not None:
        return manifest, airports_csv, runways_csv, None
    tempdir = tempfile.TemporaryDirectory(prefix="plane-alerts-airports-")
    root = Path(tempdir.name)
    airports_path = root / "airports.csv"
    runways_path = root / "runways.csv"
    _download(str(manifest["files"]["airports.csv"]["url"]), airports_path)
    _download(str(manifest["files"]["runways.csv"]["url"]), runways_path)
    return manifest, airports_path, runways_path, tempdir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--airports-csv", type=Path)
    parser.add_argument("--runways-csv", type=Path)
    args = parser.parse_args()
    if (args.airports_csv is None) != (args.runways_csv is None):
        parser.error("--airports-csv and --runways-csv must be supplied together")

    manifest, airports_csv, runways_csv, tempdir = _resolve_sources(
        args.manifest, args.airports_csv, args.runways_csv
    )
    try:
        result = build_database(
            airports_csv,
            runways_csv,
            args.output,
            manifest=manifest,
            overrides_dir=args.overrides,
        )
    finally:
        if tempdir is not None:
            tempdir.cleanup()
    print("AIRPORT_DATABASE_JSON=" + json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
