#!/usr/bin/env python3
"""Release gate for the compiled global airport/runway reference database."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "aviation" / "compiled" / "global_airports.sqlite3"
EXPECTED_SOURCE_COMMIT = "e856aa3449d7f509af0b2f853b5c814ab28d0eb5"


def main() -> int:
    if not DB.is_file():
        raise SystemExit(f"missing compiled airport database: {DB}")
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        airport_count = int(conn.execute("SELECT COUNT(*) FROM airports").fetchone()[0])
        runway_count = int(conn.execute("SELECT COUNT(*) FROM runways").fetchone()[0])
        source_commit = str(conn.execute("SELECT value FROM metadata WHERE key='source_commit'").fetchone()[0])
        if airport_count < 80000:
            raise SystemExit(f"global airport dataset unexpectedly small: {airport_count}")
        if runway_count < 35000:
            raise SystemExit(f"global runway dataset unexpectedly small: {runway_count}")
        if source_commit != EXPECTED_SOURCE_COMMIT:
            raise SystemExit(f"unexpected source commit: {source_commit}")

        required_codes = ("LTFM", "LTBA", "EGLL", "KJFK", "RJTT", "OMDB", "YSSY", "SBGR", "FAOR")
        missing = [
            code for code in required_codes
            if conn.execute(
                "SELECT 1 FROM airports WHERE ident=? OR gps_code=? LIMIT 1", (code, code)
            ).fetchone() is None
        ]
        if missing:
            raise SystemExit(f"representative worldwide airports missing: {missing}")

        ltfm = conn.execute(
            "SELECT le_ident, he_ident FROM runways WHERE airport_ident='LTFM' AND closed=0 ORDER BY le_ident"
        ).fetchall()
        ltfm_pairs = {(str(row[0]), str(row[1])) for row in ltfm}
        expected_ltfm = {
            ("16L", "34R"), ("16R", "34L"), ("17L", "35R"), ("17R", "35L"), ("18", "36")
        }
        if ltfm_pairs != expected_ltfm:
            raise SystemExit(f"LTFM override mismatch: {sorted(ltfm_pairs)}")

        # Ataturk is a primary v4.7.2 Error Museum airport. Verify the exact
        # identity used by route providers and the runway geometry that the
        # terminal classifier will consume. ICAO LTBA and IATA ISL must resolve
        # to the same active airport record; destination='ISL' is never treated
        # as a magic suppression code by itself.
        ltba = conn.execute(
            """
            SELECT ident, gps_code, iata_code, name, latitude_deg, longitude_deg, closed
            FROM airports
            WHERE ident='LTBA' OR gps_code='LTBA' OR iata_code='ISL'
            ORDER BY CASE WHEN ident='LTBA' THEN 0 WHEN gps_code='LTBA' THEN 1 ELSE 2 END
            LIMIT 1
            """
        ).fetchone()
        if ltba is None:
            raise SystemExit("LTBA/ISL airport identity missing from compiled database")
        if str(ltba["ident"]) != "LTBA" or str(ltba["iata_code"] or "") != "ISL" or bool(ltba["closed"]):
            raise SystemExit(
                "LTBA/ISL identity mismatch: "
                f"ident={ltba['ident']} gps={ltba['gps_code']} iata={ltba['iata_code']} closed={ltba['closed']}"
            )
        if not (40.0 <= float(ltba["latitude_deg"]) <= 42.0 and 27.0 <= float(ltba["longitude_deg"]) <= 30.0):
            raise SystemExit("LTBA coordinates are outside the expected Istanbul region")

        ltba_active = conn.execute(
            """
            SELECT le_ident, he_ident,
                   le_latitude_deg, le_longitude_deg, le_heading_deg,
                   he_latitude_deg, he_longitude_deg, he_heading_deg
            FROM runways
            WHERE airport_ident='LTBA' AND closed=0
            ORDER BY le_ident
            """
        ).fetchall()
        if not ltba_active:
            raise SystemExit("LTBA has no active runway geometry")
        ltba_pairs = {(str(row["le_ident"]), str(row["he_ident"])) for row in ltba_active}
        if ("05", "23") not in ltba_pairs:
            raise SystemExit(f"LTBA active 05/23 runway geometry missing: {sorted(ltba_pairs)}")
        for row in ltba_active:
            geometry = (
                row["le_latitude_deg"], row["le_longitude_deg"], row["le_heading_deg"],
                row["he_latitude_deg"], row["he_longitude_deg"], row["he_heading_deg"],
            )
            if any(value is None for value in geometry):
                raise SystemExit(
                    f"LTBA active runway {row['le_ident']}/{row['he_ident']} has incomplete endpoint geometry"
                )

        print("GLOBAL_AIRPORT_DB_JSON=" + json.dumps({
            "airports": airport_count,
            "runways": runway_count,
            "source_commit": source_commit,
            "ltfm_runways": len(ltfm_pairs),
            "ltba_iata": str(ltba["iata_code"] or ""),
            "ltba_active_runways": sorted([f"{left}/{right}" for left, right in ltba_pairs]),
        }, sort_keys=True, separators=(",", ":")))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
