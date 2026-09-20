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

        required_codes = ("LTFM", "EGLL", "KJFK", "RJTT", "OMDB", "YSSY", "SBGR", "FAOR")
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

        print("GLOBAL_AIRPORT_DB_JSON=" + json.dumps({
            "airports": airport_count,
            "runways": runway_count,
            "source_commit": source_commit,
            "ltfm_runways": len(ltfm_pairs),
        }, sort_keys=True, separators=(",", ":")))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
