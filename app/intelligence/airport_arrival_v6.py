"""Plane Alerts v6.0 explainable airport, runway-threshold and arrival/departure detector.

Evaluates candidate airports against:
- Airport boundaries and terminal elevation (AGL calculation)
- Runways, runway thresholds (RunwayEnd), and extended runway centerlines
- Aircraft position, altitude, heading/track direction, and recent motion trend
- Observer geometry (pass-before-airport vs airport-before-observer)

Ensures that:
1. Every airport/arrival decision is explainable (which airport/geometry matched,
   what track evidence indicated arrival/departure/holding/missed_approach/overflight,
   and why that changed or did not change the alert).
2. An airport classification never suppresses a genuine observer pass without
   adequate physical and geometric evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Sequence

from app.intelligence import airport_terminal_v47 as terminal_v47
from app.intelligence import trajectory as traj

AIRPORT_DETECTOR_VERSION = "6.0-airport-runway-threshold-v1"
AIRPORT_BOUNDARY_RADIUS_KM = 6.5
EXTENDED_CENTERLINE_MAX_KM = 38.0
FINAL_CROSS_TRACK_MAX_KM = 1.65
INTERCEPT_CROSS_TRACK_MAX_KM = 4.2
RUNWAY_HEADING_MAX_DELTA_DEG = 22.0
INTERCEPT_HEADING_MAX_DELTA_DEG = 45.0
OVERFLIGHT_MIN_AGL_M = 3200.0


@dataclass(slots=True, frozen=True)
class RunwayThresholdMatch:
    """Geometric match against a specific directed runway end and threshold."""

    airport_icao: str
    airport_iata: str
    runway_id: str
    threshold_lat: float
    threshold_lon: float
    runway_heading_deg: float
    distance_to_threshold_km: float
    along_track_before_threshold_km: float
    cross_track_km: float
    heading_delta_deg: float
    glidepath_angle_deg: float | None
    aligned_final: bool
    aligned_intercept: bool
    departing_from_runway: bool
    landing_rollout_cpa_to_observer_km: float | None


@dataclass(slots=True, frozen=True)
class AirportArrivalExplanation:
    """Complete, explainable airport classification and alert-modification record."""

    classification: str  # "ARRIVAL", "DEPARTURE", "HOLDING", "MISSED_APPROACH", "OVERFLIGHT", "AIRPORT_NEARBY", "NONE"
    matched_airport_icao: str
    matched_airport_iata: str
    matched_airport_name: str
    matched_geometry: str  # "runway_threshold", "extended_centerline", "airport_boundary", "terminal_corridor", "none"
    matched_runway_id: str
    airport_distance_km: float | None
    threshold_distance_km: float | None
    cross_track_km: float | None
    along_track_km: float | None
    agl_m: float | None
    vertical_rate_mps: float | None
    airport_closing_rate_km_s: float | None
    destination_code: str
    destination_matches_airport: bool
    observer_before_airport: bool
    landing_path_enters_observer_radius: bool
    track_evidence: tuple[str, ...]
    suppress_alert: bool
    alert_changed: bool
    reason_code: str
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "matched_airport_icao": self.matched_airport_icao,
            "matched_airport_iata": self.matched_airport_iata,
            "matched_airport_name": self.matched_airport_name,
            "matched_geometry": self.matched_geometry,
            "matched_runway_id": self.matched_runway_id,
            "airport_distance_km": self.airport_distance_km,
            "threshold_distance_km": self.threshold_distance_km,
            "cross_track_km": self.cross_track_km,
            "along_track_km": self.along_track_km,
            "agl_m": self.agl_m,
            "vertical_rate_mps": self.vertical_rate_mps,
            "airport_closing_rate_km_s": self.airport_closing_rate_km_s,
            "destination_code": self.destination_code,
            "destination_matches_airport": self.destination_matches_airport,
            "observer_before_airport": self.observer_before_airport,
            "landing_path_enters_observer_radius": self.landing_path_enters_observer_radius,
            "track_evidence": list(self.track_evidence),
            "suppress_alert": self.suppress_alert,
            "alert_changed": self.alert_changed,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
        }


def _finite(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _angle_delta(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _to_terminal_samples(samples: Iterable[Any]) -> list[terminal_v47.TerminalSample]:
    out: list[terminal_v47.TerminalSample] = []
    for item in samples:
        if isinstance(item, terminal_v47.TerminalSample):
            out.append(item)
            continue
        ts = _finite(getattr(item, "timestamp", None))
        lat = _finite(getattr(item, "latitude", None))
        lon = _finite(getattr(item, "longitude", None))
        if None in (ts, lat, lon):
            continue
        alt = _finite(getattr(item, "altitude_m", getattr(item, "altitude", None)))
        spd = _finite(getattr(item, "speed_kts", getattr(item, "ground_speed", None)))
        if spd is None:
            vel = _finite(getattr(item, "velocity", None))
            if vel is not None:
                spd = vel * 1.9438444924406
        hdg = _finite(getattr(item, "heading_deg", getattr(item, "heading", None)))
        vr = _finite(getattr(item, "vertical_rate_mps", None))
        age = _finite(getattr(item, "position_age_s", 0.0)) or 0.0
        out.append(
            terminal_v47.TerminalSample(
                timestamp=float(ts),
                latitude=float(lat),
                longitude=float(lon),
                altitude_m=alt,
                speed_kts=spd,
                heading_deg=hdg,
                vertical_rate_mps=vr,
                position_age_s=float(age),
            )
        )
    out.sort(key=lambda s: s.timestamp)
    return out


def match_runway_threshold(
    *,
    aircraft_lat: float,
    aircraft_lon: float,
    aircraft_agl_m: float | None,
    aircraft_heading_deg: float | None,
    airport: terminal_v47.AirportGeometry,
    observer_lat: float | None = None,
    observer_lon: float | None = None,
) -> RunwayThresholdMatch | None:
    """Evaluate aircraft geometry against every runway threshold of an airport."""
    if not airport.runways or aircraft_heading_deg is None:
        return None

    best_match: RunwayThresholdMatch | None = None
    best_rank: tuple[int, float] = (99, math.inf)

    for runway in airport.runways:
        pairs = ((runway.end_a, runway.end_b), (runway.end_b, runway.end_a))
        for threshold, opposite in pairs:
            dist_to_thresh = traj.haversine_km(
                aircraft_lat,
                aircraft_lon,
                threshold.latitude,
                threshold.longitude,
            )
            if dist_to_thresh > EXTENDED_CENTERLINE_MAX_KM + 8.0:
                continue

            heading_delta = abs(_angle_delta(float(aircraft_heading_deg), float(threshold.heading_deg)))
            # Approach inbound azimuth from aircraft to threshold should align with runway heading
            bearing_to_thresh = traj.bearing_deg(
                aircraft_lat,
                aircraft_lon,
                threshold.latitude,
                threshold.longitude,
            )
            inbound_angle_err = abs(_angle_delta(bearing_to_thresh, float(threshold.heading_deg)))
            rad_err = math.radians(inbound_angle_err)
            cross_track_km = abs(dist_to_thresh * math.sin(rad_err))
            along_track_before_km = dist_to_thresh * math.cos(rad_err)

            # Check if departing past the threshold/opposite end in runway direction
            bearing_from_thresh = traj.bearing_deg(
                threshold.latitude,
                threshold.longitude,
                aircraft_lat,
                aircraft_lon,
            )
            outbound_err = abs(_angle_delta(bearing_from_thresh, float(threshold.heading_deg)))
            outbound_cross_km = abs(dist_to_thresh * math.sin(math.radians(outbound_err)))
            departing_from_rw = (
                heading_delta <= 28.0
                and outbound_err <= 30.0
                and outbound_cross_km <= 2.5
                and dist_to_thresh <= 28.0
            )

            aligned_final = (
                0.2 <= along_track_before_km <= EXTENDED_CENTERLINE_MAX_KM
                and inbound_angle_err <= 25.0
                and cross_track_km <= FINAL_CROSS_TRACK_MAX_KM
                and heading_delta <= RUNWAY_HEADING_MAX_DELTA_DEG
            )
            aligned_intercept = (
                not aligned_final
                and 0.5 <= along_track_before_km <= EXTENDED_CENTERLINE_MAX_KM
                and inbound_angle_err <= 45.0
                and cross_track_km <= INTERCEPT_CROSS_TRACK_MAX_KM
                and heading_delta <= INTERCEPT_HEADING_MAX_DELTA_DEG
            )

            glidepath_deg: float | None = None
            if aircraft_agl_m is not None and along_track_before_km > 0.25:
                glidepath_deg = math.degrees(
                    math.atan2(max(0.0, float(aircraft_agl_m)), along_track_before_km * 1000.0)
                )

            rollout_cpa_km: float | None = None
            if observer_lat is not None and observer_lon is not None:
                # Sample points from current aircraft position to threshold and along runway rollout to opposite end
                sample_distances: list[float] = []
                for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
                    p_lat = aircraft_lat + (threshold.latitude - aircraft_lat) * frac
                    p_lon = aircraft_lon + (threshold.longitude - aircraft_lon) * frac
                    sample_distances.append(traj.haversine_km(p_lat, p_lon, observer_lat, observer_lon))
                for frac in (0.25, 0.5, 0.75, 1.0):
                    p_lat = threshold.latitude + (opposite.latitude - threshold.latitude) * frac
                    p_lon = threshold.longitude + (opposite.longitude - threshold.longitude) * frac
                    sample_distances.append(traj.haversine_km(p_lat, p_lon, observer_lat, observer_lon))
                rollout_cpa_km = min(sample_distances) if sample_distances else None

            priority = 0 if aligned_final else 1 if aligned_intercept else 2 if departing_from_rw else 3
            score = cross_track_km + 0.04 * heading_delta + 0.02 * dist_to_thresh
            if (priority, score) < best_rank:
                best_rank = (priority, score)
                best_match = RunwayThresholdMatch(
                    airport_icao=airport.icao,
                    airport_iata=airport.iata,
                    runway_id=threshold.identifier,
                    threshold_lat=threshold.latitude,
                    threshold_lon=threshold.longitude,
                    runway_heading_deg=threshold.heading_deg,
                    distance_to_threshold_km=round(dist_to_thresh, 3),
                    along_track_before_threshold_km=round(along_track_before_km, 3),
                    cross_track_km=round(cross_track_km, 3),
                    heading_delta_deg=round(heading_delta, 2),
                    glidepath_angle_deg=round(glidepath_deg, 2) if glidepath_deg is not None else None,
                    aligned_final=aligned_final,
                    aligned_intercept=aligned_intercept,
                    departing_from_runway=departing_from_rw,
                    landing_rollout_cpa_to_observer_km=round(rollout_cpa_km, 3) if rollout_cpa_km is not None else None,
                )
    return best_match


def _airport_closing_rate(
    samples: Sequence[terminal_v47.TerminalSample],
    target_lat: float,
    target_lon: float,
) -> float | None:
    if len(samples) < 2:
        return None
    oldest = samples[0]
    newest = samples[-1]
    dt = newest.timestamp - oldest.timestamp
    if dt < 4.0:
        return None
    d_old = traj.haversine_km(oldest.latitude, oldest.longitude, target_lat, target_lon)
    d_new = traj.haversine_km(newest.latitude, newest.longitude, target_lat, target_lon)
    return (d_new - d_old) / dt


def _sustained_descent(samples: Sequence[terminal_v47.TerminalSample], latest_vr: float | None) -> bool:
    if latest_vr is not None and latest_vr <= -1.2:
        return True
    usable = list(samples)[-5:]
    vr_count = sum(1 for s in usable if s.vertical_rate_mps is not None and s.vertical_rate_mps <= -0.45)
    alts = [s.altitude_m for s in usable if s.altitude_m is not None]
    alt_drop = (float(alts[0]) - float(alts[-1])) if len(alts) >= 2 else 0.0
    return vr_count >= 2 or alt_drop >= 60.0


def _sustained_climb(samples: Sequence[terminal_v47.TerminalSample], latest_vr: float | None) -> bool:
    if latest_vr is not None and latest_vr >= 2.0:
        return True
    usable = list(samples)[-5:]
    vr_count = sum(1 for s in usable if s.vertical_rate_mps is not None and s.vertical_rate_mps >= 1.2)
    alts = [s.altitude_m for s in usable if s.altitude_m is not None]
    alt_gain = (float(alts[-1]) - float(alts[0])) if len(alts) >= 2 else 0.0
    return vr_count >= 2 or alt_gain >= 75.0


def evaluate_airport_arrival_v6(
    *,
    aircraft: Any,
    prediction: Any,
    samples: Iterable[Any],
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    candidate_airport: terminal_v47.AirportGeometry | Any | None = None,
    destination_airport: Any | None = None,
    route_plausible: bool = False,
    now: float | None = None,
) -> AirportArrivalExplanation:
    """Evaluate airport/runway/threshold evidence and explain its effect on the alert."""
    t_samples = _to_terminal_samples(samples)
    lat = _finite(getattr(aircraft, "latitude", None))
    lon = _finite(getattr(aircraft, "longitude", None))
    alt = _finite(getattr(aircraft, "altitude", getattr(aircraft, "altitude_m", None)))
    hdg = _finite(getattr(aircraft, "heading", getattr(aircraft, "heading_deg", None)))
    vr = _finite(getattr(aircraft, "vertical_rate_mps", None))
    if t_samples:
        latest = t_samples[-1]
        lat = lat if lat is not None else latest.latitude
        lon = lon if lon is not None else latest.longitude
        alt = alt if alt is not None else latest.altitude_m
        hdg = hdg if hdg is not None else latest.heading_deg
        vr = vr if vr is not None else latest.vertical_rate_mps

    dest_code = str(getattr(destination_airport, "code", "") or getattr(destination_airport, "iata", "") or getattr(destination_airport, "icao", "") or "").upper().strip()

    if lat is None or lon is None:
        return AirportArrivalExplanation(
            classification="NONE",
            matched_airport_icao="",
            matched_airport_iata="",
            matched_airport_name="",
            matched_geometry="none",
            matched_runway_id="",
            airport_distance_km=None,
            threshold_distance_km=None,
            cross_track_km=None,
            along_track_km=None,
            agl_m=None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=None,
            destination_code=dest_code,
            destination_matches_airport=False,
            observer_before_airport=False,
            landing_path_enters_observer_radius=False,
            track_evidence=("missing_position",),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_NONE_NO_POSITION",
            explanation="Aircraft position is unavailable; airport classification not applicable.",
        )

    airport: terminal_v47.AirportGeometry | None = None
    if isinstance(candidate_airport, terminal_v47.AirportGeometry):
        airport = candidate_airport
    elif candidate_airport is not None:
        airport = terminal_v47.airport_for_info(candidate_airport)
    if airport is None and destination_airport is not None:
        airport = terminal_v47.airport_for_info(destination_airport)
    if airport is None:
        airport = terminal_v47.nearest_airport(float(lat), float(lon), max_distance_km=65.0)

    if airport is None:
        return AirportArrivalExplanation(
            classification="NONE",
            matched_airport_icao="",
            matched_airport_iata="",
            matched_airport_name="",
            matched_geometry="none",
            matched_runway_id="",
            airport_distance_km=None,
            threshold_distance_km=None,
            cross_track_km=None,
            along_track_km=None,
            agl_m=None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=None,
            destination_code=dest_code,
            destination_matches_airport=False,
            observer_before_airport=False,
            landing_path_enters_observer_radius=False,
            track_evidence=("no_nearby_or_destination_airport",),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_NONE_OUTSIDE_TERMINAL_RANGE",
            explanation="No destination or terminal airport matched within range; live trajectory remains authoritative.",
        )

    airport_dist_km = traj.haversine_km(float(lat), float(lon), airport.latitude, airport.longitude)
    observer_dist_km = traj.haversine_km(float(lat), float(lon), float(user_lat), float(user_lon))
    observer_to_airport_km = traj.haversine_km(float(user_lat), float(user_lon), airport.latitude, airport.longitude)
    agl_m = (float(alt) - float(airport.elevation_m or 0.0)) if alt is not None else None
    closing_rate = _airport_closing_rate(t_samples, airport.latitude, airport.longitude)
    descending = _sustained_descent(t_samples, vr)
    climbing = _sustained_climb(t_samples, vr)

    airport_codes = {airport.icao.upper().strip(), airport.iata.upper().strip()} - {""}
    dest_codes = {
        str(getattr(destination_airport, "icao", "") or "").upper().strip(),
        str(getattr(destination_airport, "iata", "") or "").upper().strip(),
        dest_code,
    } - {""}
    dest_matches = bool(airport_codes & dest_codes)

    rw_match = match_runway_threshold(
        aircraft_lat=float(lat),
        aircraft_lon=float(lon),
        aircraft_agl_m=agl_m,
        aircraft_heading_deg=hdg,
        airport=airport,
        observer_lat=float(user_lat),
        observer_lon=float(user_lon),
    )

    v47_assessment = (
        terminal_v47.assess_terminal(t_samples, airport, now=now)
        if t_samples
        else None
    )

    evidence: list[str] = []
    if dest_matches:
        evidence.append(f"destination_matches:{airport.icao or airport.iata}")
    if airport_dist_km <= AIRPORT_BOUNDARY_RADIUS_KM:
        evidence.append(f"inside_airport_boundary:{airport_dist_km:.1f}km")
    if rw_match is not None:
        if rw_match.aligned_final:
            evidence.append(
                f"aligned_runway_threshold:{rw_match.runway_id}"
                f"(xtk={rw_match.cross_track_km:.2f}km,dist={rw_match.distance_to_threshold_km:.1f}km)"
            )
        elif rw_match.aligned_intercept:
            evidence.append(
                f"extended_centerline_intercept:{rw_match.runway_id}"
                f"(xtk={rw_match.cross_track_km:.2f}km,hdg_err={rw_match.heading_delta_deg:.1f}deg)"
            )
        elif rw_match.departing_from_runway:
            evidence.append(f"outbound_runway_vector:{rw_match.runway_id}")
    if descending:
        evidence.append(f"sustained_descent(vr={vr if vr is not None else 0.0:.1f}mps)")
    if climbing:
        evidence.append(f"sustained_climb(vr={vr if vr is not None else 0.0:.1f}mps)")
    if closing_rate is not None:
        evidence.append(f"airport_range_rate:{closing_rate:+.3f}km/s")
    if agl_m is not None:
        evidence.append(f"agl:{agl_m:.0f}m")

    # Determine if observer pass occurs genuinely before reaching the airport landing area
    cpa_time = _finite(getattr(prediction, "time_to_cpa_s", None))
    entry_time = _finite(getattr(prediction, "radius_entry_s", None))
    candidate_time = entry_time if entry_time is not None else cpa_time
    speed_kts = _finite(getattr(aircraft, "ground_speed", None))
    if speed_kts is None:
        vel = _finite(getattr(aircraft, "velocity", None))
        speed_kts = vel * 1.9438444924406 if vel is not None else None
    speed_km_s = (speed_kts or 0.0) * traj.KNOTS_TO_KM_S

    target_lat = rw_match.threshold_lat if (rw_match and (rw_match.aligned_final or rw_match.aligned_intercept)) else airport.latitude
    target_lon = rw_match.threshold_lon if (rw_match and (rw_match.aligned_final or rw_match.aligned_intercept)) else airport.longitude
    dist_to_landing_target_km = traj.haversine_km(float(lat), float(lon), target_lat, target_lon)
    eta_to_landing_s = (dist_to_landing_target_km / speed_km_s) if speed_km_s > 0.03 else None

    # Check projected path points against landing area and observer CPA
    path_points = list(getattr(prediction, "path", None) or [])
    first_landing_area_s: float | None = None
    cpa_dist_to_airport_km: float | None = None
    for pt in path_points:
        pt_s = _finite(getattr(pt, "seconds", None))
        pt_lat = _finite(getattr(pt, "latitude", None))
        pt_lon = _finite(getattr(pt, "longitude", None))
        if None in (pt_s, pt_lat, pt_lon):
            continue
        d_ap = traj.haversine_km(float(pt_lat), float(pt_lon), target_lat, target_lon)
        if d_ap <= 4.5 and first_landing_area_s is None:
            first_landing_area_s = float(pt_s)
        if cpa_time is not None and abs(float(pt_s) - cpa_time) <= 3.0:
            cpa_dist_to_airport_km = d_ap

    observer_inside_airport_radius = observer_to_airport_km <= float(alert_radius_km)
    landing_rollout_cpa = rw_match.landing_rollout_cpa_to_observer_km if rw_match else None
    landing_path_enters_observer = bool(
        observer_inside_airport_radius
        or (
            landing_rollout_cpa is not None
            and landing_rollout_cpa <= float(alert_radius_km)
        )
    )

    # Observer is genuinely before airport if aircraft reaches observer radius/CPA materially before airport
    # and the observer CPA is closer to the airport than the aircraft's current position (aircraft passes observer on its way to airport)
    observer_before_airport = False
    if observer_dist_km <= float(alert_radius_km):
        observer_before_airport = True
    elif (
        candidate_time is not None
        and eta_to_landing_s is not None
        and candidate_time + 20.0 < eta_to_landing_s
        and (cpa_dist_to_airport_km is None or cpa_dist_to_airport_km < dist_to_landing_target_km)
    ):
        observer_before_airport = True

    matched_geom = (
        "runway_threshold"
        if (rw_match and rw_match.aligned_final)
        else "extended_centerline"
        if (rw_match and rw_match.aligned_intercept)
        else "airport_boundary"
        if airport_dist_km <= AIRPORT_BOUNDARY_RADIUS_KM
        else "terminal_corridor"
        if airport_dist_km <= 85.0
        else "none"
    )
    matched_rw_id = rw_match.runway_id if rw_match else (v47_assessment.runway_id if v47_assessment else "")

    # 1. MISSED_APPROACH / GO_AROUND
    if (
        (v47_assessment and (v47_assessment.go_around or v47_assessment.missed_approach))
        or (
            climbing
            and agl_m is not None
            and agl_m <= 2800.0
            and airport_dist_km <= 22.0
            and (
                (closing_rate is not None and closing_rate > 0.004)
                or (v47_assessment and v47_assessment.state in {"GO_AROUND", "MISSED_APPROACH"})
            )
            and dest_matches
        )
    ):
        evidence.append("missed_approach_or_go_around_climb")
        return AirportArrivalExplanation(
            classification="MISSED_APPROACH",
            matched_airport_icao=airport.icao,
            matched_airport_iata=airport.iata,
            matched_airport_name=airport.name,
            matched_geometry=matched_geom,
            matched_runway_id=matched_rw_id,
            airport_distance_km=round(airport_dist_km, 2),
            threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
            cross_track_km=rw_match.cross_track_km if rw_match else None,
            along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
            agl_m=round(agl_m, 1) if agl_m is not None else None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
            destination_code=dest_code or airport.code,
            destination_matches_airport=dest_matches,
            observer_before_airport=observer_before_airport,
            landing_path_enters_observer_radius=landing_path_enters_observer,
            track_evidence=tuple(evidence),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_RELEASE_MISSED_APPROACH",
            explanation=(
                f"Missed approach / go-around detected near {airport.icao or airport.iata} "
                f"(climbing at {vr if vr is not None else 0.0:+.1f} m/s); arrival suppression released."
            ),
        )

    # 2. HOLDING
    if v47_assessment and v47_assessment.probable_holding and v47_assessment.holding_score >= 0.75:
        evidence.append(f"holding_loop(score={v47_assessment.holding_score:.2f})")
        should_hold = observer_dist_km > float(alert_radius_km) and not observer_before_airport
        return AirportArrivalExplanation(
            classification="HOLDING",
            matched_airport_icao=airport.icao,
            matched_airport_iata=airport.iata,
            matched_airport_name=airport.name,
            matched_geometry="terminal_corridor",
            matched_runway_id=matched_rw_id,
            airport_distance_km=round(airport_dist_km, 2),
            threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
            cross_track_km=rw_match.cross_track_km if rw_match else None,
            along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
            agl_m=round(agl_m, 1) if agl_m is not None else None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
            destination_code=dest_code or airport.code,
            destination_matches_airport=dest_matches,
            observer_before_airport=observer_before_airport,
            landing_path_enters_observer_radius=landing_path_enters_observer,
            track_evidence=tuple(evidence),
            suppress_alert=should_hold,
            alert_changed=should_hold,
            reason_code="AIRPORT_SUPPRESS_HOLDING_PATTERN" if should_hold else "AIRPORT_HOLDING_NEAR_OBSERVER",
            explanation=(
                f"Aircraft is in a multi-sample terminal holding pattern near {airport.icao or airport.iata}; "
                "temporary straight-line tangent does not represent an inbound observer pass."
            ),
        )

    # 3. DEPARTURE (climbing away from airport boundary / runway)
    if (
        climbing
        and airport_dist_km <= 45.0
        and not dest_matches
        and (
            (closing_rate is not None and closing_rate >= 0.005)
            or (rw_match is not None and rw_match.departing_from_runway)
            or airport_dist_km <= 18.0
        )
    ):
        evidence.append("climbing_departure_away_from_airport")
        return AirportArrivalExplanation(
            classification="DEPARTURE",
            matched_airport_icao=airport.icao,
            matched_airport_iata=airport.iata,
            matched_airport_name=airport.name,
            matched_geometry=matched_geom,
            matched_runway_id=matched_rw_id,
            airport_distance_km=round(airport_dist_km, 2),
            threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
            cross_track_km=rw_match.cross_track_km if rw_match else None,
            along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
            agl_m=round(agl_m, 1) if agl_m is not None else None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
            destination_code=dest_code,
            destination_matches_airport=False,
            observer_before_airport=True,
            landing_path_enters_observer_radius=False,
            track_evidence=tuple(evidence),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_ALLOW_DEPARTURE",
            explanation=(
                f"Aircraft is departing and climbing away from {airport.icao or airport.iata} "
                f"(runway {matched_rw_id or 'terminal'}); not an arrival—live observer trajectory remains authoritative."
            ),
        )

    # 4. OVERFLIGHT (high altitude or level transit over/near airport without descent or destination match)
    if (
        not dest_matches
        and not descending
        and (
            (agl_m is not None and agl_m >= OVERFLIGHT_MIN_AGL_M)
            or (rw_match is None or not (rw_match.aligned_final or rw_match.aligned_intercept))
        )
    ):
        evidence.append("level_or_high_altitude_overflight")
        return AirportArrivalExplanation(
            classification="OVERFLIGHT",
            matched_airport_icao=airport.icao,
            matched_airport_iata=airport.iata,
            matched_airport_name=airport.name,
            matched_geometry=matched_geom,
            matched_runway_id=matched_rw_id,
            airport_distance_km=round(airport_dist_km, 2),
            threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
            cross_track_km=rw_match.cross_track_km if rw_match else None,
            along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
            agl_m=round(agl_m, 1) if agl_m is not None else None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
            destination_code=dest_code,
            destination_matches_airport=False,
            observer_before_airport=observer_before_airport,
            landing_path_enters_observer_radius=False,
            track_evidence=tuple(evidence),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_ALLOW_OVERFLIGHT",
            explanation=(
                f"Aircraft is transiting/overflying near {airport.icao or airport.iata} without descent "
                "or runway-threshold arrival alignment; live trajectory remains authoritative."
            ),
        )

    # 5. Check strong runway-threshold or destination-backed ARRIVAL
    has_runway_final = bool(
        rw_match
        and (rw_match.aligned_final or rw_match.aligned_intercept)
        and descending
        and (agl_m is None or agl_m <= 4200.0)
        and (closing_rate is None or closing_rate <= -0.005)
    )
    has_terminal_arrival = bool(
        dest_matches
        and airport_dist_km <= 180.0
        and (descending or (agl_m is not None and agl_m <= 8500.0))
        and not climbing
    )

    if has_runway_final or has_terminal_arrival:
        # Never suppress if the aircraft is already inside the observer's alert radius
        if observer_dist_km <= float(alert_radius_km):
            return AirportArrivalExplanation(
                classification="ARRIVAL",
                matched_airport_icao=airport.icao,
                matched_airport_iata=airport.iata,
                matched_airport_name=airport.name,
                matched_geometry=matched_geom,
                matched_runway_id=matched_rw_id,
                airport_distance_km=round(airport_dist_km, 2),
                threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
                cross_track_km=rw_match.cross_track_km if rw_match else None,
                along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
                agl_m=round(agl_m, 1) if agl_m is not None else None,
                vertical_rate_mps=vr,
                airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
                destination_code=dest_code or airport.code,
                destination_matches_airport=dest_matches,
                observer_before_airport=True,
                landing_path_enters_observer_radius=True,
                track_evidence=tuple(evidence + ["physical_presence_inside_observer_radius"]),
                suppress_alert=False,
                alert_changed=False,
                reason_code="AIRPORT_ARRIVAL_INSIDE_OBSERVER_RADIUS",
                explanation=(
                    f"Aircraft is arriving at {airport.icao or airport.iata} but is already physically "
                    "inside the observer alert radius; alert is preserved."
                ),
            )

        # Never suppress if the airport/runway landing rollout itself enters the observer radius
        if observer_inside_airport_radius:
            return AirportArrivalExplanation(
                classification="ARRIVAL",
                matched_airport_icao=airport.icao,
                matched_airport_iata=airport.iata,
                matched_airport_name=airport.name,
                matched_geometry=matched_geom,
                matched_runway_id=matched_rw_id,
                airport_distance_km=round(airport_dist_km, 2),
                threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
                cross_track_km=rw_match.cross_track_km if rw_match else None,
                along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
                agl_m=round(agl_m, 1) if agl_m is not None else None,
                vertical_rate_mps=vr,
                airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
                destination_code=dest_code or airport.code,
                destination_matches_airport=dest_matches,
                observer_before_airport=True,
                landing_path_enters_observer_radius=True,
                track_evidence=tuple(evidence + ["airport_inside_observer_radius"]),
                suppress_alert=False,
                alert_changed=False,
                reason_code="AIRPORT_ARRIVAL_AIRPORT_INSIDE_RADIUS",
                explanation=(
                    f"Destination airport {airport.icao or airport.iata} lies inside observer radius "
                    f"({observer_to_airport_km:.1f} km <= {alert_radius_km:.1f} km); arrival pass is genuine."
                ),
            )

        # Check whether aircraft lands at airport/threshold BEFORE reaching observer or turns away to airport
        reaches_landing_first = bool(
            (first_landing_area_s is not None and candidate_time is not None and first_landing_area_s + 5.0 < candidate_time)
            or (eta_to_landing_s is not None and candidate_time is not None and eta_to_landing_s + 35.0 < candidate_time and descending)
        )
        cpa_moves_away_from_airport = bool(
            cpa_dist_to_airport_km is not None
            and cpa_dist_to_airport_km >= airport_dist_km + max(5.0, min(18.0, airport_dist_km * 0.10))
        )
        runway_final_misses_observer = bool(
            has_runway_final
            and rw_match is not None
            and rw_match.landing_rollout_cpa_to_observer_km is not None
            and rw_match.landing_rollout_cpa_to_observer_km > float(alert_radius_km) + max(1.5, float(alert_radius_km) * 0.15)
            and rw_match.distance_to_threshold_km < observer_dist_km
        )

        if reaches_landing_first or cpa_moves_away_from_airport or runway_final_misses_observer:
            why = (
                f"aligned with runway {matched_rw_id} threshold ({rw_match.distance_to_threshold_km:.1f} km to threshold, "
                f"cross-track {rw_match.cross_track_km:.2f} km) and terminates at {airport.icao or airport.iata} before observer"
                if runway_final_misses_observer and rw_match is not None
                else f"projected path reaches {airport.icao or airport.iata} landing area before observer pass"
                if reaches_landing_first
                else f"observer CPA would carry aircraft away from terminal destination {airport.icao or airport.iata}"
            )
            return AirportArrivalExplanation(
                classification="ARRIVAL",
                matched_airport_icao=airport.icao,
                matched_airport_iata=airport.iata,
                matched_airport_name=airport.name,
                matched_geometry=matched_geom,
                matched_runway_id=matched_rw_id,
                airport_distance_km=round(airport_dist_km, 2),
                threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
                cross_track_km=rw_match.cross_track_km if rw_match else None,
                along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
                agl_m=round(agl_m, 1) if agl_m is not None else None,
                vertical_rate_mps=vr,
                airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
                destination_code=dest_code or airport.code,
                destination_matches_airport=dest_matches,
                observer_before_airport=False,
                landing_path_enters_observer_radius=False,
                track_evidence=tuple(evidence),
                suppress_alert=True,
                alert_changed=True,
                reason_code="AIRPORT_SUPPRESS_VERIFIED_ARRIVAL",
                explanation=f"Verified arrival at {airport.icao or airport.iata}: {why}.",
            )

        # Otherwise, the arrival passes the observer BEFORE reaching the airport!
        return AirportArrivalExplanation(
            classification="AIRPORT_NEARBY",
            matched_airport_icao=airport.icao,
            matched_airport_iata=airport.iata,
            matched_airport_name=airport.name,
            matched_geometry=matched_geom,
            matched_runway_id=matched_rw_id,
            airport_distance_km=round(airport_dist_km, 2),
            threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
            cross_track_km=rw_match.cross_track_km if rw_match else None,
            along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
            agl_m=round(agl_m, 1) if agl_m is not None else None,
            vertical_rate_mps=vr,
            airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
            destination_code=dest_code or airport.code,
            destination_matches_airport=dest_matches,
            observer_before_airport=True,
            landing_path_enters_observer_radius=True,
            track_evidence=tuple(evidence + ["genuine_pass_before_airport"]),
            suppress_alert=False,
            alert_changed=False,
            reason_code="AIRPORT_ALLOW_PASS_BEFORE_LANDING",
            explanation=(
                f"Arrival toward {airport.icao or airport.iata} (runway {matched_rw_id or 'approach'}) "
                "genuinely passes the observer before reaching the airport landing area; alert is preserved."
            ),
        )

    return AirportArrivalExplanation(
        classification="AIRPORT_NEARBY",
        matched_airport_icao=airport.icao,
        matched_airport_iata=airport.iata,
        matched_airport_name=airport.name,
        matched_geometry=matched_geom,
        matched_runway_id=matched_rw_id,
        airport_distance_km=round(airport_dist_km, 2),
        threshold_distance_km=rw_match.distance_to_threshold_km if rw_match else None,
        cross_track_km=rw_match.cross_track_km if rw_match else None,
        along_track_km=rw_match.along_track_before_threshold_km if rw_match else None,
        agl_m=round(agl_m, 1) if agl_m is not None else None,
        vertical_rate_mps=vr,
        airport_closing_rate_km_s=round(closing_rate, 4) if closing_rate is not None else None,
        destination_code=dest_code,
        destination_matches_airport=dest_matches,
        observer_before_airport=observer_before_airport,
        landing_path_enters_observer_radius=landing_path_enters_observer,
        track_evidence=tuple(evidence),
        suppress_alert=False,
        alert_changed=False,
        reason_code="AIRPORT_ALLOW_INSUFFICIENT_ARRIVAL_EVIDENCE",
        explanation=(
            f"Traffic near {airport.icao or airport.iata} lacks sufficient runway-threshold or "
            "terminal-arrival evidence to suppress a qualifying observer pass."
        ),
    )
