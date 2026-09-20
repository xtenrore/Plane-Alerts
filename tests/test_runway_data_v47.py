from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence.runway_data_v47 import (
    LTFM_OPERATIONAL_RUNWAY_PAIRS,
    install_current_runway_data_v47,
)


def test_ltfm_production_runway_filter_matches_verified_operational_pairs():
    corrected = install_current_runway_data_v47()
    pairs = {(runway.end_a.identifier, runway.end_b.identifier) for runway in corrected.runways}
    assert pairs == LTFM_OPERATIONAL_RUNWAY_PAIRS
    assert len(corrected.runways) == 5
    assert ("09", "27") not in pairs
    assert terminal.LTFM is corrected
    assert terminal.nearest_airport(corrected.latitude, corrected.longitude, max_distance_km=1.0) is corrected
