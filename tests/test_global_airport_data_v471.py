from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.intelligence.airport_repository import AirportRepository
from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence import global_airports_v471 as global_airports
from scripts.build_airport_database import build_database


AIRPORT_HEADERS = [
    "id", "ident", "type", "name", "latitude_deg", "longitude_deg", "elevation_ft",
    "continent", "iso_country", "iso_region", "municipality", "scheduled_service",
    "gps_code", "iata_code", "local_code", "home_link", "wikipedia_link", "keywords",
]
RUNWAY_HEADERS = [
    "id", "airport_ref", "airport_ident", "length_ft", "width_ft", "surface", "lighted", "closed",
    "le_ident", "le_latitude_deg", "le_longitude_deg", "le_elevation_ft", "le_heading_degT",
    "le_displaced_threshold_ft", "he_ident", "he_latitude_deg", "he_longitude_deg",
    "he_elevation_ft", "he_heading_degT", "he_displaced_threshold_ft",
]


def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture()
def airport_db(tmp_path: Path) -> Path:
    airports = tmp_path / "airports.csv"
    runways = tmp_path / "runways.csv"
    output = tmp_path / "airports.sqlite3"
    _write_csv(airports, AIRPORT_HEADERS, [
        {
            "id": 1, "ident": "EGLL", "type": "large_airport", "name": "London Heathrow Airport",
            "latitude_deg": 51.4700, "longitude_deg": -0.4543, "elevation_ft": 83,
            "continent": "EU", "iso_country": "GB", "iso_region": "GB-ENG", "municipality": "London",
            "scheduled_service": "yes", "gps_code": "EGLL", "iata_code": "LHR", "local_code": "",
        },
        {
            "id": 2, "ident": "KJFK", "type": "large_airport", "name": "John F Kennedy International Airport",
            "latitude_deg": 40.6413, "longitude_deg": -73.7781, "elevation_ft": 13,
            "continent": "NA", "iso_country": "US", "iso_region": "US-NY", "municipality": "New York",
            "scheduled_service": "yes", "gps_code": "KJFK", "iata_code": "JFK", "local_code": "",
        },
        {
            "id": 3, "ident": "US-TEST", "type": "small_airport", "name": "No ICAO Test Field",
            "latitude_deg": 40.7, "longitude_deg": -73.9, "elevation_ft": 100,
            "continent": "NA", "iso_country": "US", "iso_region": "US-NY", "municipality": "Test",
            "scheduled_service": "no", "gps_code": "", "iata_code": "", "local_code": "TST1",
        },
        {
            "id": 4, "ident": "OLD1", "type": "closed", "name": "Closed Test Airport",
            "latitude_deg": 40.65, "longitude_deg": -73.77, "elevation_ft": 10,
            "continent": "NA", "iso_country": "US", "iso_region": "US-NY", "municipality": "Test",
            "scheduled_service": "no", "gps_code": "", "iata_code": "", "local_code": "OLD1",
        },
    ])
    _write_csv(runways, RUNWAY_HEADERS, [
        {
            "id": 10, "airport_ref": 1, "airport_ident": "EGLL", "length_ft": 12799, "width_ft": 164,
            "surface": "ASP", "lighted": 1, "closed": 0,
            "le_ident": "09L", "le_latitude_deg": 51.4775, "le_longitude_deg": -0.489,
            "le_elevation_ft": 75, "le_heading_degT": 89.5, "le_displaced_threshold_ft": "",
            "he_ident": "27R", "he_latitude_deg": 51.4775, "he_longitude_deg": -0.434,
            "he_elevation_ft": 78, "he_heading_degT": 269.5, "he_displaced_threshold_ft": "",
        },
        {
            "id": 11, "airport_ref": 2, "airport_ident": "KJFK", "length_ft": 12079, "width_ft": 200,
            "surface": "ASP", "lighted": 1, "closed": 0,
            "le_ident": "04L", "le_latitude_deg": 40.620, "le_longitude_deg": -73.785,
            "le_elevation_ft": 10, "le_heading_degT": 44.0, "le_displaced_threshold_ft": "",
            "he_ident": "22R", "he_latitude_deg": 40.655, "he_longitude_deg": -73.760,
            "he_elevation_ft": 10, "he_heading_degT": 224.0, "he_displaced_threshold_ft": "",
        },
    ])
    build_database(
        airports,
        runways,
        output,
        manifest={"source": "fixture", "commit": "fixture", "minimum_counts": {"airports": 4, "runways": 2}},
        overrides_dir=None,
    )
    return output


def test_repository_resolves_icao_iata_and_local_codes_with_runways(airport_db: Path):
    repo = AirportRepository(airport_db)
    egll = repo.by_code("EGLL")
    lhr = repo.by_code("lhr")
    local = repo.by_code("tst1")
    assert egll is not None and lhr is not None and egll.ident == lhr.ident == "EGLL"
    assert egll.runways and egll.runways[0].le_ident == "09L" and egll.runways[0].he_ident == "27R"
    assert local is not None and local.ident == "US-TEST"


def test_repository_nearest_uses_local_spatial_cells_and_excludes_closed_airports(airport_db: Path):
    repo = AirportRepository(airport_db)
    nearest = repo.nearest(40.6413, -73.7781, max_distance_km=20)
    assert nearest is not None and nearest.ident == "KJFK"
    assert repo.by_code("OLD1") is None
    assert repo.by_code("OLD1", include_closed=True) is not None


def test_global_layer_supplies_full_non_ltfm_runway_geometry(monkeypatch: pytest.MonkeyPatch, airport_db: Path):
    repo = AirportRepository(airport_db)
    monkeypatch.setattr(global_airports, "airport_repository", repo)
    terminal._airports.pop("EGLL", None)
    info = SimpleNamespace(icao="EGLL", iata="LHR", latitude=51.47, longitude=-0.454, name="Heathrow")
    airport = global_airports.airport_for_info(info)
    assert airport is not None
    assert airport.icao == "EGLL"
    assert airport.iata == "LHR"
    assert len(airport.runways) == 1
    assert airport.runways[0].end_a.identifier == "09L"


def test_compiled_reference_replaces_stale_cached_geometry_on_first_code_lookup(monkeypatch: pytest.MonkeyPatch, airport_db: Path):
    repo = AirportRepository(airport_db)
    monkeypatch.setattr(global_airports, "airport_repository", repo)
    stale = terminal.AirportGeometry(
        "EGLL", "LHR", "stale", 51.4700, -0.4543, 25.0,
        (
            terminal.RunwayGeometry(
                "EGLL",
                terminal.RunwayEnd("00", 51.47, -0.46, 0.0),
                terminal.RunwayEnd("18", 51.47, -0.44, 180.0),
            ),
        ),
    )
    monkeypatch.setitem(terminal._airports, "EGLL", stale)
    info = SimpleNamespace(icao="EGLL", iata="LHR", latitude=51.47, longitude=-0.454, name="Heathrow")
    airport = global_airports.airport_for_info(info)
    assert airport is not None
    assert airport.name == "London Heathrow Airport"
    assert {(r.end_a.identifier, r.end_b.identifier) for r in airport.runways} == {("09L", "27R")}


def test_global_layer_can_resolve_nearest_airport_without_destination_metadata(monkeypatch: pytest.MonkeyPatch, airport_db: Path):
    repo = AirportRepository(airport_db)
    monkeypatch.setattr(global_airports, "airport_repository", repo)
    terminal._airports.pop("KJFK", None)
    airport = global_airports.nearest_airport(40.641, -73.778, max_distance_km=50)
    assert airport is not None
    assert airport.icao == "KJFK"
    assert airport.runways
