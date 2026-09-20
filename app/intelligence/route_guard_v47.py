"""Plane Alerts v4.7 airport/runway context layered on the existing route guard.

The v4.3 cancellation latch and v4.2 terminal ensemble remain authoritative.
New runway/holding/base/final hypotheses are shadow-only in v4.7.0. The sole
live override is fail-safe: strong observed go-around/missed-approach evidence
may release an old expected-landing-turn hold so fresh physical geometry can be
re-evaluated. It never creates a pass on its own.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

from app.aircraft.providers import ProviderManager
from app.intelligence import airport_terminal_v47 as airport_v47
from app.intelligence import requalification_guard_v43 as v43
from app.intelligence import route_guard_v2 as v2
from app.intelligence import route_guard_v42 as v42
from app.intelligence import route_history as route_mod

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL_QUERY_PROVIDERS = ProviderManager.query_providers


@dataclass(slots=True, frozen=True)
class RouteGateResultV47(v42.RouteGateResultV42):
    airport_icao: str = ""
    airport_iata: str = ""
    terminal_state_v47: str = "OUTSIDE_TERMINAL"
    terminal_state_confidence_v47: str = "Uncertain"
    terminal_confidence_penalty_v47: float = 0.0
    vector_state_v47: str = "INSUFFICIENT"
    runway_candidate_v47: str = ""
    runway_family_v47: str = ""
    runway_confidence_v47: str = "Uncertain"
    runway_support_v47: int = 0
    runway_changed_v47: bool = False
    probable_holding_v47: bool = False
    go_around_v47: bool = False
    missed_approach_v47: bool = False
    runway_path_cpa_km_v47: float | None = None
    route_cluster_runway_v47: str = ""
    route_cluster_runway_support_v47: int = 0
    recent_movement_cluster_v47: str = ""
    recent_movement_support_v47: float = 0.0
    airport_shadow_hold_v47: bool = False
    airport_shadow_reason_v47: str = ""
    airport_model_version_v47: str = airport_v47.AIRPORT_SHADOW_MODEL_VERSION
    authoritative_override_v47: str = ""


async def _query_providers_v47(
    self: ProviderManager,
    latitude: float,
    longitude: float,
    radius_nm: int = 250,
    provider_names: list[str] | None = None,
):
    merged, by_provider = await _ORIGINAL_QUERY_PROVIDERS(
        self,
        latitude,
        longitude,
        radius_nm=radius_nm,
        provider_names=provider_names,
    )
    # Coalesced and bounded. No I/O or clustering is awaited from the alert path.
    airport_v47.enqueue_provider_snapshot(merged)
    return merged, by_provider


def _fallback_samples(ac: Any, current_samples: Iterable[Any], *, now: float) -> list[airport_v47.TerminalSample]:
    samples: list[airport_v47.TerminalSample] = []
    for item in current_samples:
        try:
            samples.append(airport_v47.TerminalSample(
                timestamp=float(getattr(item, "timestamp")),
                latitude=float(getattr(item, "latitude")),
                longitude=float(getattr(item, "longitude")),
                altitude_m=_float_or_none(getattr(item, "altitude_m", None)),
                speed_kts=_float_or_none(getattr(item, "speed_kts", None)),
                heading_deg=_float_or_none(getattr(item, "heading_deg", None)),
                vertical_rate_mps=_float_or_none(getattr(item, "vertical_rate_mps", None)),
                position_age_s=float(getattr(item, "position_age_s", 0.0) or 0.0),
            ))
        except (AttributeError, TypeError, ValueError):
            continue
    if samples:
        return samples
    try:
        speed = getattr(ac, "ground_speed", None)
        samples.append(airport_v47.TerminalSample(
            timestamp=now - float(getattr(ac, "position_age_s", 0.0) or 0.0),
            latitude=float(ac.latitude),
            longitude=float(ac.longitude),
            altitude_m=_float_or_none(getattr(ac, "altitude", None)),
            speed_kts=_float_or_none(speed),
            heading_deg=_float_or_none(getattr(ac, "heading", None)),
            vertical_rate_mps=_float_or_none(getattr(ac, "vertical_rate_mps", None)),
            position_age_s=float(getattr(ac, "position_age_s", 0.0) or 0.0),
        ))
    except (AttributeError, TypeError, ValueError):
        pass
    return samples


def _float_or_none(value: Any) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _choose_airport(ac: Any, destination: Any | None) -> airport_v47.AirportGeometry | None:
    dest_airport = airport_v47.airport_for_info(destination)
    if dest_airport is not None:
        try:
            distance = v42.haversine_km(float(ac.latitude), float(ac.longitude), dest_airport.latitude, dest_airport.longitude)
        except (AttributeError, TypeError, ValueError):
            distance = math.inf
        if distance <= 140.0:
            return dest_airport
    try:
        return airport_v47.nearest_airport(float(ac.latitude), float(ac.longitude), max_distance_km=100.0)
    except (AttributeError, TypeError, ValueError):
        return None


def _can_release_expected_landing_hold(result: route_mod.RouteGateResult, assessment: airport_v47.TerminalAssessment, pred: Any) -> bool:
    if not (assessment.go_around or assessment.missed_approach):
        return False
    if bool(getattr(pred, "stale", False)) or not bool(getattr(pred, "enters_alert_radius", False)):
        return False
    state = str(getattr(result, "qualification_state", "") or "")
    # Never bypass the confirmed cancellation latch. Fresh physical radius entry
    # already has its own v4.3/v4.4 recovery path.
    if state == "CANCEL_LATCHED":
        return False
    return state in {"EXPECTED_TURN_PENDING", "TURN_STARTED", "TURN_CONFIRMED"}


async def evaluate_route_v47(
    self: route_mod.RouteHistoryService,
    ac: Any,
    pred: Any,
    *,
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    current_samples: Iterable[Any],
) -> route_mod.RouteGateResult:
    current_samples = list(current_samples)
    base = await v43.evaluate_route_v43(
        self,
        ac,
        pred,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=alert_radius_km,
        current_samples=current_samples,
    )
    key = route_mod.normalize_flight_key(getattr(ac, "callsign", ""))
    route, _ = v2._cached_route(self, key) if key else (None, False)
    destination = route.destination if route else None
    airport = _choose_airport(ac, destination)

    now = time.time()
    extended = airport_v47.history_for(str(getattr(ac, "icao24", "") or ""))
    fallback = _fallback_samples(ac, current_samples, now=now)
    if fallback:
        latest_fallback = fallback[-1].timestamp
        if not extended or latest_fallback > extended[-1].timestamp + 0.001:
            extended = [*extended, *[item for item in fallback if not extended or item.timestamp > extended[-1].timestamp + 0.001]]
    if len(extended) < len(fallback):
        extended = fallback

    assessment = airport_v47.assess_terminal(
        extended,
        airport,
        now=now,
        observer_lat=user_lat,
        observer_lon=user_lon,
    )
    inference = airport_v47.infer_runway_configuration(airport, now=now) if airport is not None else airport_v47.RunwayInference("")
    history = await v2._historical_paths_cached(self, key) if key else []
    route_cluster_runway, route_cluster_support, route_cluster_total = airport_v47.associate_historical_runways(history, airport)
    movement_clusters = airport_v47.recent_movement_clusters(airport, now=now)
    movement_cluster = movement_clusters[0] if movement_clusters else {}
    shadow_hold, shadow_reason = airport_v47.shadow_terminal_hold(
        assessment,
        pred=pred,
        alert_radius_km=alert_radius_km,
    )

    authoritative_override = ""
    updated_base = base
    if _can_release_expected_landing_hold(base, assessment, pred):
        authoritative_override = "GO_AROUND_LIVE_REEVALUATION"
        updated_base = replace(
            base,
            suppress_alert=False,
            expected_turn_pending=False,
            qualification_state="GO_AROUND_LIVE_REEVALUATION",
            reason=(
                "v4.7 observed go-around/missed-approach invalidated the prior landing-turn expectation; "
                "fresh live geometry remains authoritative"
            ),
        )

    payload = airport_v47.diagnostic_payload(
        assessment,
        inference,
        shadow_hold=shadow_hold,
        shadow_reason=shadow_reason,
        route_cluster_runway=route_cluster_runway,
        route_cluster_support=route_cluster_support,
        route_cluster_total=route_cluster_total,
        authoritative_override=authoritative_override,
    )
    payload["recent_movement_cluster"] = str(movement_cluster.get("label") or "")
    payload["recent_movement_support"] = float(movement_cluster.get("decayed_support") or 0.0)
    payload["recent_movement_sample_count"] = int(movement_cluster.get("sample_count") or 0)
    airport_v47.record_prediction_diagnostics(pred, payload)
    logger.debug("v47_terminal_decision %s", json.dumps({
        "aircraft": str(getattr(ac, "icao24", "") or ""),
        "flight": str(getattr(ac, "callsign", "") or ""),
        **payload,
        "live_cpa_km": round(float(getattr(pred, "projected_closest_km", math.inf)), 3),
        "live_current_km": round(float(getattr(pred, "current_distance_km", math.inf)), 3),
        "baseline_suppress": bool(base.suppress_alert),
        "effective_suppress": bool(updated_base.suppress_alert),
    }, sort_keys=True, separators=(",", ":")))

    return RouteGateResultV47(
        **asdict(updated_base),
        airport_icao=assessment.airport_icao,
        airport_iata=assessment.airport_iata,
        terminal_state_v47=assessment.state,
        terminal_state_confidence_v47=assessment.state_confidence,
        terminal_confidence_penalty_v47=assessment.confidence_penalty,
        vector_state_v47=assessment.vector_state,
        runway_candidate_v47=assessment.runway_id,
        runway_family_v47=assessment.runway_family,
        runway_confidence_v47=inference.confidence,
        runway_support_v47=inference.supporting_aircraft,
        runway_changed_v47=inference.changed,
        probable_holding_v47=assessment.probable_holding,
        go_around_v47=assessment.go_around,
        missed_approach_v47=assessment.missed_approach,
        runway_path_cpa_km_v47=assessment.runway_path_cpa_km,
        route_cluster_runway_v47=route_cluster_runway,
        route_cluster_runway_support_v47=route_cluster_support,
        recent_movement_cluster_v47=str(movement_cluster.get("label") or ""),
        recent_movement_support_v47=float(movement_cluster.get("decayed_support") or 0.0),
        airport_shadow_hold_v47=shadow_hold,
        airport_shadow_reason_v47=shadow_reason,
        authoritative_override_v47=authoritative_override,
    )


def install_route_guard_v47() -> None:
    global _INSTALLED, _ORIGINAL_QUERY_PROVIDERS
    if _INSTALLED:
        return
    _ORIGINAL_QUERY_PROVIDERS = ProviderManager.query_providers
    ProviderManager.query_providers = _query_providers_v47
    route_mod.RouteHistoryService.evaluate = evaluate_route_v47
    _INSTALLED = True
    logger.info(
        "Plane Alerts v4.7 airport terminal intelligence enabled: model=%s runway/terminal holds=shadow go-around_fail_safe=active",
        airport_v47.AIRPORT_SHADOW_MODEL_VERSION,
    )
