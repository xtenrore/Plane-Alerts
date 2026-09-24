import json
from pathlib import Path


def test_v42_error_museum_fixture_preserves_production_failure_evidence():
    path = Path("docs/error_museum/v42-ist-arrival-false-alert-2026-09-18.json")
    payload = json.loads(path.read_text())
    assert payload["classification"] == "decision-level reconstruction"
    assert payload["raw_adsb_track_available"] is False
    encounters = {item["flight"]: item for item in payload["encounters"]}
    assert encounters["THY2GN"]["old_system"]["stored_projected_cpa_km"] < 5.0
    assert "17.26" in encounters["THY2GN"]["old_system"]["2026-09-18T16:53:09Z"]
    assert encounters["THY7ER"]["old_system"]["stored_projected_cpa_km"] < 8.0
    assert payload["v42_expected_behavior"]["stale_data"].startswith("uncertainty")


def test_v554_error_museum_contract_moves_arrival_authority_out_of_route_history():
    path = Path("docs/error_museum/v42-ist-arrival-false-alert-2026-09-18.json")
    payload = json.loads(path.read_text())
    expected = payload["v554_destination_path_expected_behavior"]
    assert "only live arrival authority" in expected["authority_model"]
    assert "5-10 seconds" in expected["THY2GN"]
    assert "single physically strong nearby terminal destination" in expected["THY2GN"]
    assert "without using yesterday's route as a veto" in expected["THY7ER"]
    assert "ambigu" in expected["provider_conflict_or_outage"]
    assert "fail open" in expected["provider_conflict_or_outage"]
    assert "always overrides" in expected["physical_entry"]
    assert "releases destination suppression" in expected["diversion_or_go_around"]
