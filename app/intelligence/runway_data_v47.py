"""Authoritative operational-runway filters for Plane Alerts v4.7.

The geometric thresholds live in ``airport_terminal_v47``. This module only
controls which maintained runway pairs are eligible for production inference.
The LTFM set below follows the current Türkiye AIP operational runway set
reviewed for the v4.7 release; secondary datasets may contain planned or stale
runway records that must not silently become production evidence.
"""
from __future__ import annotations

from dataclasses import replace

from app.intelligence import airport_terminal_v47 as terminal

LTFM_OPERATIONAL_RUNWAY_PAIRS = frozenset({
    ("16L", "34R"),
    ("16R", "34L"),
    ("17L", "35R"),
    ("17R", "35L"),
    ("18", "36"),
})


def install_current_runway_data_v47() -> terminal.AirportGeometry:
    """Filter LTFM to the currently verified operational runway pairs.

    This deliberately fails closed during startup if the underlying maintained
    geometry is missing an expected pair, rather than continuing with silently
    incomplete runway intelligence. Plane Alerts can still be rolled back to the
    previous tested release in that situation.
    """
    source = terminal.LTFM
    selected = tuple(
        runway
        for runway in source.runways
        if (runway.end_a.identifier, runway.end_b.identifier) in LTFM_OPERATIONAL_RUNWAY_PAIRS
    )
    found = {(runway.end_a.identifier, runway.end_b.identifier) for runway in selected}
    if found != LTFM_OPERATIONAL_RUNWAY_PAIRS:
        missing = sorted(LTFM_OPERATIONAL_RUNWAY_PAIRS - found)
        raise RuntimeError(f"LTFM maintained runway geometry missing verified pairs: {missing}")

    corrected = replace(source, runways=selected)
    terminal.LTFM = corrected
    terminal.register_airport(corrected)
    return corrected
