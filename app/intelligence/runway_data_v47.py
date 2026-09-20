"""Authoritative operational-runway filters for Plane Alerts v4.7.

The geometric thresholds live in ``airport_terminal_v47``. This module only
controls which maintained runway pairs are eligible for production inference.
The LTFM set below follows the current Türkiye AIP operational runway set
reviewed for the v4.7 release; secondary datasets may contain planned or stale
runway records that must not silently become production evidence.
"""
from __future__ import annotations

from dataclasses import replace
import logging

from app.intelligence import airport_terminal_v47 as terminal

logger = logging.getLogger(__name__)

LTFM_OPERATIONAL_RUNWAY_PAIRS = frozenset({
    ("16L", "34R"),
    ("16R", "34L"),
    ("17L", "35R"),
    ("17R", "35L"),
    ("18", "36"),
})


def verified_runways(
    source: terminal.AirportGeometry,
    expected_pairs: frozenset[tuple[str, str]],
) -> tuple[tuple[terminal.RunwayGeometry, ...], bool]:
    """Return only verified runway pairs and whether the set is complete.

    Incomplete maintained geometry is treated as unavailable rather than as a
    partially authoritative runway model. Generic terminal intelligence can
    continue without runway-specific inference.
    """
    selected = tuple(
        runway
        for runway in source.runways
        if (runway.end_a.identifier, runway.end_b.identifier) in expected_pairs
    )
    found = {(runway.end_a.identifier, runway.end_b.identifier) for runway in selected}
    return selected, found == expected_pairs


def install_current_runway_data_v47() -> terminal.AirportGeometry:
    """Install the currently verified LTFM operational runway set.

    Airport/runway intelligence is supporting evidence and must never prevent
    core monitoring from starting. If the maintained geometry ever becomes
    incomplete, runway-specific inference is disabled for LTFM and the service
    continues with generic terminal context until the data layer is corrected.
    """
    source = terminal.LTFM
    selected, complete = verified_runways(source, LTFM_OPERATIONAL_RUNWAY_PAIRS)
    if not complete:
        found = {(runway.end_a.identifier, runway.end_b.identifier) for runway in selected}
        missing = sorted(LTFM_OPERATIONAL_RUNWAY_PAIRS - found)
        logger.error(
            "ltfm_runway_geometry_incomplete missing=%s runway_specific_inference=disabled",
            missing,
        )
        selected = ()

    corrected = replace(source, runways=selected)
    terminal.LTFM = corrected
    terminal.register_airport(corrected)
    logger.info(
        "v47_ltfm_runway_data installed=%d expected=%d complete=%s",
        len(selected),
        len(LTFM_OPERATIONAL_RUNWAY_PAIRS),
        complete,
    )
    return corrected
