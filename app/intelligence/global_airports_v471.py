"""Plane Alerts v4.7.1 global airport/runway data integration.

This layer completes v4.7 airport intelligence with an on-disk worldwide
reference database while leaving the existing terminal classifier untouched.
Fresh geometry remains authoritative and database failure degrades to the
existing metadata/LTFM behavior rather than stopping monitoring.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from app.intelligence import airport_terminal_v47 as terminal
from app.intelligence.airport_repository import AirportReference, airport_repository
from app.intelligence import trajectory as traj

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL_AIRPORT_FOR_INFO = terminal.airport_for_info
_ORIGINAL_NEAREST_AIRPORT = terminal.nearest_airport


def _geometry(reference: AirportReference) -> terminal.AirportGeometry:
    icao = reference.gps_code if len(reference.gps_code) == 4 else reference.ident if len(reference.ident) == 4 else ""
    runways = tuple(
        terminal.RunwayGeometry(
            icao or reference.ident,
            terminal.RunwayEnd(
                runway.le_ident,
                runway.le_latitude_deg,
                runway.le_longitude_deg,
                runway.le_heading_deg,
            ),
            terminal.RunwayEnd(
                runway.he_ident,
                runway.he_latitude_deg,
                runway.he_longitude_deg,
                runway.he_heading_deg,
            ),
        )
        for runway in reference.runways
    )
    return terminal.AirportGeometry(
        icao=icao or reference.ident,
        iata=reference.iata_code,
        name=reference.name,
        latitude=reference.latitude_deg,
        longitude=reference.longitude_deg,
        elevation_m=reference.elevation_m,
        runways=runways,
    )


def _runway_signature(airport: terminal.AirportGeometry) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (runway.end_a.identifier, runway.end_b.identifier)
        for runway in airport.runways
    ))


def _cached_airport(icao: str, iata: str) -> terminal.AirportGeometry | None:
    for airport in tuple(terminal._airports.values()):
        if (icao and airport.icao == icao) or (iata and airport.iata == iata):
            return airport
    return None


def airport_for_info(info: Any | None) -> terminal.AirportGeometry | None:
    if info is None:
        return None
    icao = str(getattr(info, "icao", "") or "").upper().strip()
    iata = str(getattr(info, "iata", "") or "").upper().strip()

    # A matching record in the pinned local database wins over a pre-existing
    # in-memory object. This is important for maintained overrides such as LTFM:
    # an older built-in geometry must not beat the verified compiled override on
    # the first lookup merely because it was imported earlier.
    reference = airport_repository.by_code(icao) if icao else None
    if reference is None and iata:
        reference = airport_repository.by_code(iata)
    if reference is not None:
        compiled = _geometry(reference)
        cached = _cached_airport(icao, iata)
        if (
            cached is not None
            and cached.icao == compiled.icao
            and _runway_signature(cached) == _runway_signature(compiled)
        ):
            return cached
        return terminal.register_airport(compiled)

    cached = _cached_airport(icao, iata)
    if cached is not None:
        return cached

    # Some route sources provide coordinates but weak/missing codes. Resolve a
    # very-near reference airport before falling back to metadata-only context.
    try:
        lat = float(getattr(info, "latitude", None))
        lon = float(getattr(info, "longitude", None))
    except (TypeError, ValueError):
        lat = lon = math.nan
    if math.isfinite(lat) and math.isfinite(lon):
        reference = airport_repository.nearest(lat, lon, max_distance_km=5.0)
        if reference is not None:
            return terminal.register_airport(_geometry(reference))
    return _ORIGINAL_AIRPORT_FOR_INFO(info)


def nearest_airport(lat: float, lon: float, *, max_distance_km: float = 120.0) -> terminal.AirportGeometry | None:
    memory = _ORIGINAL_NEAREST_AIRPORT(lat, lon, max_distance_km=max_distance_km)
    reference = airport_repository.nearest(lat, lon, max_distance_km=max_distance_km)
    compiled = _geometry(reference) if reference is not None else None
    if memory is None:
        return terminal.register_airport(compiled) if compiled is not None else None
    if compiled is None:
        return memory

    memory_distance = traj.haversine_km(lat, lon, memory.latitude, memory.longitude)
    global_distance = traj.haversine_km(lat, lon, compiled.latitude, compiled.longitude)
    same_airport = memory.icao == compiled.icao or (memory.iata and memory.iata == compiled.iata)
    if (
        same_airport
        and abs(memory_distance - global_distance) <= 1e-6
        and _runway_signature(memory) == _runway_signature(compiled)
    ):
        # Preserve the identity of an already-installed verified object. Existing
        # v4.7 code/tests rely on this for LTFM, and equivalent compiled geometry
        # does not justify replacing it.
        return memory

    global_airport = terminal.register_airport(compiled)
    return global_airport if global_distance <= memory_distance else memory


def install_global_airport_data_v471() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    terminal.airport_for_info = airport_for_info
    terminal.nearest_airport = nearest_airport
    _INSTALLED = True
    if airport_repository.available:
        airports, runways = airport_repository.counts()
        logger.info(
            "Plane Alerts global airport data enabled: airports=%d runways=%d source_commit=%s runtime_network=false",
            airports,
            runways,
            airport_repository.metadata("source_commit"),
        )
    else:
        logger.error(
            "Plane Alerts global airport data unavailable; continuing with bounded metadata fallback"
        )


install_global_airport_data_v471()
