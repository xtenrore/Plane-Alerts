"""Plane Alerts v4.7.2 narrow authoritative initial terminal-arrival hold.

The purpose of this guard is deliberately small: prevent the *first* predictive
notification when fresh physical evidence strongly indicates a terminal arrival
whose temporary straight-line CPA points at the observer. Destination metadata
is supporting evidence only and can never suppress an alert by itself.

The hold is fail-open. It releases immediately for stale/insufficient evidence,
a go-around or climb, a trajectory change away from the airport approach, a
landing path through the runway that can genuinely pass the observer, or fresh physical
entry into the user's configured radius.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any, Sequence

from app.intelligence import airport_terminal_v47 as airport_v47
from app.intelligence import trajectory as traj

RUNWAY_ARRIVAL_STATES = {
    "DOWNWIND_TO_BASE",
    "BASE_TURN",
    "FINAL_INTERCEPT",
    "ESTABLISHED_FINAL",
}


@dataclass(slots=True, frozen=True)
class TerminalArrivalHoldDecision:
    hold: bool
    code: str
    reason: str
    score: float = 0.0
    destination_match: bool = False
    fresh_age_s: float | None = None
    airport_trend_km_s: float | None = None
    heading_error_deg: float | None = None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _angle_delta(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


def _destination_matches(destination: Any | None, airport: airport_v47.AirportGeometry | None) -> bool:
    if destination is None or airport is None:
        return False
    destination_codes = {
        str(getattr(destination, "icao", "") or "").upper().strip(),
        str(getattr(destination, "iata", "") or "").upper().strip(),
    } - {""}
    airport_codes = {airport.icao.upper().strip(), airport.iata.upper().strip()} - {""}
    return bool(destination_codes & airport_codes)


def _fresh_age_s(samples: Sequence[airport_v47.TerminalSample], now: float) -> float | None:
    if not samples:
        return None
    latest = samples[-1]
    timestamp_age = max(0.0, now - float(latest.timestamp))
    reported_age = max(0.0, float(latest.position_age_s or 0.0))
    return max(timestamp_age, reported_age)


def _airport_trend_km_s(
    samples: Sequence[airport_v47.TerminalSample],
    airport: airport_v47.AirportGeometry,
) -> float | None:
    usable = list(samples)[-6:]
    if len(usable) < 3:
        return None
    first, last = usable[0], usable[-1]
    dt = float(last.timestamp) - float(first.timestamp)
    if dt < 12.0:
        return None
    first_distance = traj.haversine_km(first.latitude, first.longitude, airport.latitude, airport.longitude)
    last_distance = traj.haversine_km(last.latitude, last.longitude, airport.latitude, airport.longitude)
    return (last_distance - first_distance) / dt


def _descent_is_sustained(samples: Sequence[airport_v47.TerminalSample]) -> bool:
    usable = list(samples)[-5:]
    if len(usable) < 3:
        return False
    descending = sum(
        sample.vertical_rate_mps is not None and float(sample.vertical_rate_mps) <= -0.4
        for sample in usable
    )
    altitudes = [sample for sample in usable if sample.altitude_m is not None]
    altitude_drop = 0.0
    if len(altitudes) >= 2:
        altitude_drop = float(altitudes[0].altitude_m) - float(altitudes[-1].altitude_m)
    return descending >= 2 or altitude_drop >= 75.0


def evaluate_initial_terminal_hold(
    assessment: airport_v47.TerminalAssessment,
    *,
    airport: airport_v47.AirportGeometry | None,
    pred: Any,
    samples: Sequence[airport_v47.TerminalSample],
    destination: Any | None,
    route_plausible: bool,
    alert_radius_km: float,
    baseline_terminal_score: float = 0.0,
    baseline_expected_turn_state: str = "",
    now: float | None = None,
) -> TerminalArrivalHoldDecision:
    """Return whether the *initial* user-visible alert should be held.

    This function never creates a pass or cancellation. The caller applies the
    decision only before the first notification; already-active alerts continue
    to use the established lifecycle/cancellation guards.
    """
    now = time.time() if now is None else float(now)
    radius = max(0.1, float(alert_radius_km))
    current = _finite(getattr(pred, "current_distance_km", None))
    current = math.inf if current is None else current

    if bool(getattr(pred, "stale", False)):
        return TerminalArrivalHoldDecision(False, "RELEASE_STALE", "stale observation cannot create a terminal-arrival hold")
    if not bool(getattr(pred, "enters_alert_radius", False)):
        return TerminalArrivalHoldDecision(False, "NO_LIVE_PASS", "live trajectory does not currently qualify for the observer radius")
    if current <= radius:
        return TerminalArrivalHoldDecision(False, "RELEASE_OBSERVED_INSIDE", "fresh physical radius entry is authoritative")
    if bool(getattr(pred, "already_passed", False)) or str(getattr(pred, "state", "")) == "Passed":
        return TerminalArrivalHoldDecision(False, "RELEASE_PHYSICAL_PASS", "observed pass state is authoritative")
    if assessment.go_around or assessment.missed_approach or assessment.state in {"GO_AROUND", "MISSED_APPROACH"}:
        return TerminalArrivalHoldDecision(False, "RELEASE_GO_AROUND", "go-around or missed approach invalidates the terminal-arrival hold")
    if str(baseline_expected_turn_state or "") == "TURN_DID_NOT_OCCUR":
        return TerminalArrivalHoldDecision(False, "RELEASE_TRAJECTORY_CHANGED", "expected airport turn did not occur; live geometry regains authority")
    if airport is None or not assessment.terminal_area:
        return TerminalArrivalHoldDecision(False, "NO_TERMINAL_CONTEXT", "aircraft is not in a usable terminal-area context")

    ordered = sorted(samples, key=lambda sample: sample.timestamp)
    if len(ordered) < 4 or ordered[-1].timestamp - ordered[0].timestamp < 15.0:
        return TerminalArrivalHoldDecision(False, "INSUFFICIENT_HISTORY", "not enough fresh terminal history for authoritative qualification hold")

    age = _fresh_age_s(ordered, now)
    if age is None or age > 15.0:
        return TerminalArrivalHoldDecision(
            False,
            "RELEASE_NOT_FRESH",
            "terminal-arrival evidence is not fresh enough for an authoritative hold",
            fresh_age_s=age,
        )

    latest = ordered[-1]
    latest_vr = _finite(latest.vertical_rate_mps)
    if latest_vr is not None and latest_vr >= 1.5:
        return TerminalArrivalHoldDecision(
            False,
            "RELEASE_CLIMBING",
            "fresh climb evidence invalidates the arrival hold",
            fresh_age_s=age,
        )

    trend = _airport_trend_km_s(ordered, airport)
    if trend is not None and trend >= 0.004:
        return TerminalArrivalHoldDecision(
            False,
            "RELEASE_TRAJECTORY_CHANGED",
            "aircraft is no longer closing on the candidate airport",
            fresh_age_s=age,
            airport_trend_km_s=trend,
        )

    heading = _finite(latest.heading_deg)
    heading_error: float | None = None
    if heading is not None:
        airport_bearing = traj.bearing_deg(latest.latitude, latest.longitude, airport.latitude, airport.longitude)
        heading_error = abs(_angle_delta(airport_bearing, heading))

    runway_state = (
        assessment.state in RUNWAY_ARRIVAL_STATES
        and assessment.state_confidence in {"Medium", "High"}
    )
    if not runway_state and heading_error is not None and heading_error > 70.0:
        return TerminalArrivalHoldDecision(
            False,
            "RELEASE_TRAJECTORY_CHANGED",
            "fresh heading has diverged from the airport arrival path",
            fresh_age_s=age,
            airport_trend_km_s=trend,
            heading_error_deg=heading_error,
        )

    margin = radius + max(2.0, radius * 0.15)
    if (
        runway_state
        and assessment.runway_landing_cpa_km is not None
        and float(assessment.runway_landing_cpa_km) <= margin
    ):
        return TerminalArrivalHoldDecision(
            False,
            "RELEASE_RUNWAY_PATH_CAN_PASS",
            "landing path through the runway can still physically enter the observer radius",
            fresh_age_s=age,
            airport_trend_km_s=trend,
            heading_error_deg=heading_error,
        )

    destination_match = _destination_matches(destination, airport)
    if not destination_match and not runway_state:
        return TerminalArrivalHoldDecision(
            False,
            "NO_STRONG_AIRPORT_IDENTITY",
            "destination does not match the candidate airport and no strong runway-relative arrival state exists",
            destination_match=False,
            fresh_age_s=age,
            airport_trend_km_s=trend,
            heading_error_deg=heading_error,
        )

    agl = None if latest.altitude_m is None else float(latest.altitude_m) - float(airport.elevation_m or 0.0)
    speed = _finite(latest.speed_kts)
    low = agl is not None and agl <= 4500.0
    slow = speed is not None and speed <= 320.0
    descending = _descent_is_sustained(ordered)
    closing_airport = trend is not None and trend <= -0.008
    heading_to_airport = heading_error is not None and heading_error <= 50.0

    # Pre-turn arrival suppression is intentionally strict. A matching ISL/LTBA
    # destination alone is insufficient: the aircraft must also be low, slow,
    # descending, closing on LTBA and physically pointed toward its terminal area.
    if not runway_state and not (
        destination_match
        and route_plausible
        and low
        and slow
        and descending
        and closing_airport
        and heading_to_airport
    ):
        return TerminalArrivalHoldDecision(
            False,
            "TERMINAL_EVIDENCE_BELOW_THRESHOLD",
            "terminal-arrival evidence is not strong enough for an authoritative initial hold",
            destination_match=destination_match,
            fresh_age_s=age,
            airport_trend_km_s=trend,
            heading_error_deg=heading_error,
        )

    # A runway-relative arrival is also required to be physically low and
    # descending. If a runway CPA is known, it must miss the observer by the
    # configured margin; otherwise the hold stays fail-open.
    if runway_state:
        if not (low and descending):
            return TerminalArrivalHoldDecision(
                False,
                "RUNWAY_EVIDENCE_BELOW_THRESHOLD",
                "runway-relative state lacks low/descent evidence",
                destination_match=destination_match,
                fresh_age_s=age,
                airport_trend_km_s=trend,
                heading_error_deg=heading_error,
            )
        if assessment.runway_landing_cpa_km is None:
            return TerminalArrivalHoldDecision(
                False,
                "RUNWAY_CPA_UNKNOWN",
                "runway-relative continuation is not known well enough to hold the initial alert",
                destination_match=destination_match,
                fresh_age_s=age,
                airport_trend_km_s=trend,
                heading_error_deg=heading_error,
            )
        if float(assessment.runway_landing_cpa_km) <= margin:
            return TerminalArrivalHoldDecision(
                False,
                "RELEASE_RUNWAY_PATH_CAN_PASS",
                "landing path through the runway can still physically enter the observer radius",
                destination_match=destination_match,
                fresh_age_s=age,
                airport_trend_km_s=trend,
                heading_error_deg=heading_error,
            )

    score = 0.0
    score += 0.22 if destination_match else 0.0
    score += 0.10 if route_plausible and destination_match else 0.0
    score += 0.08 if assessment.terminal_area else 0.0
    score += 0.20 if runway_state else 0.0
    score += 0.12 if low else 0.0
    score += 0.08 if slow else 0.0
    score += 0.14 if descending else 0.0
    score += 0.12 if closing_airport else 0.0
    score += 0.07 if heading_to_airport else 0.0
    score += min(0.10, max(0.0, float(baseline_terminal_score or 0.0)) * 0.10)
    score = min(1.0, score)

    threshold = 0.74 if runway_state else 0.82
    if score < threshold:
        return TerminalArrivalHoldDecision(
            False,
            "TERMINAL_SCORE_BELOW_THRESHOLD",
            f"terminal-arrival evidence score {score:.2f} is below authoritative threshold {threshold:.2f}",
            score=score,
            destination_match=destination_match,
            fresh_age_s=age,
            airport_trend_km_s=trend,
            heading_error_deg=heading_error,
        )

    state_label = assessment.state if runway_state else "PRETURN_TERMINAL_ARRIVAL"
    return TerminalArrivalHoldDecision(
        True,
        "HOLD_STRONG_TERMINAL_ARRIVAL",
        (
            f"fresh {state_label.lower()} evidence for {airport.icao or airport.iata}: "
            "low/slow/descent airport approach is stronger than temporary straight-line observer CPA"
        ),
        score=score,
        destination_match=destination_match,
        fresh_age_s=age,
        airport_trend_km_s=trend,
        heading_error_deg=heading_error,
    )
