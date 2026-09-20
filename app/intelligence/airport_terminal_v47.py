"""Deterministic Plane Alerts v4.7 airport/runway/terminal context.

The existing live trajectory remains authoritative. New runway, turn, holding,
and airport-cluster conclusions are shadow evidence; they never create a pass or
hard-veto fresh physical observations.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Iterable, Sequence

from app.intelligence import trajectory as traj
from app.intelligence.prediction_v46 import turn_evidence

AIRPORT_SHADOW_MODEL_VERSION = "4.7-terminal-shadow-1"
_HISTORY_TTL_S = 720.0
_HISTORY_MAX_AIRCRAFT = 4096
_HISTORY_MAX_SAMPLES = 128
_EVIDENCE_TTL_S = 1500.0
_MAX_EVIDENCE = 256
_MAX_SNAPSHOT = 768
_DIAG_TTL_S = 180.0
_DIAG_MAX = 4096


@dataclass(slots=True, frozen=True)
class RunwayEnd:
    identifier: str
    latitude: float
    longitude: float
    heading_deg: float

    @property
    def family(self) -> str:
        digits = "".join(ch for ch in self.identifier if ch.isdigit())
        return digits[:2] or self.identifier


@dataclass(slots=True, frozen=True)
class RunwayGeometry:
    airport_icao: str
    end_a: RunwayEnd
    end_b: RunwayEnd

    @property
    def ends(self) -> tuple[RunwayEnd, RunwayEnd]:
        return self.end_a, self.end_b


@dataclass(slots=True, frozen=True)
class AirportGeometry:
    icao: str
    iata: str
    name: str
    latitude: float
    longitude: float
    elevation_m: float = 0.0
    runways: tuple[RunwayGeometry, ...] = ()

    @property
    def directed_ends(self) -> tuple[RunwayEnd, ...]:
        return tuple(end for runway in self.runways for end in runway.ends)


@dataclass(slots=True, frozen=True)
class TerminalSample:
    timestamp: float
    latitude: float
    longitude: float
    altitude_m: float | None
    speed_kts: float | None
    heading_deg: float | None
    vertical_rate_mps: float | None
    position_age_s: float = 0.0


@dataclass(slots=True, frozen=True)
class RunwayEvidence:
    timestamp: float
    icao24: str
    movement: str
    runway_id: str
    runway_family: str
    score: float


@dataclass(slots=True)
class MovementCluster:
    label: str
    movement: str
    runway_family: str
    first_seen: float
    last_seen: float
    sample_count: int = 0
    aircraft_last_seen: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class RunwayInference:
    airport_icao: str
    runway_family: str = ""
    runway_id: str = ""
    confidence: str = "Uncertain"
    supporting_aircraft: int = 0
    total_aircraft: int = 0
    arrivals: int = 0
    departures: int = 0
    dominance: float = 0.0
    changed: bool = False
    evidence_age_s: float | None = None


@dataclass(slots=True, frozen=True)
class TerminalAssessment:
    airport_icao: str
    airport_iata: str
    terminal_area: bool
    airport_distance_km: float
    state: str
    state_confidence: str
    confidence_penalty: float
    vector_state: str
    runway_id: str
    runway_family: str
    runway_confidence: str
    runway_support_count: int
    probable_holding: bool
    holding_score: float
    go_around: bool
    missed_approach: bool
    runway_path_cpa_km: float | None
    reasons: tuple[str, ...] = ()


def _rw(icao: str, a: tuple[str, float, float, float], b: tuple[str, float, float, float]) -> RunwayGeometry:
    return RunwayGeometry(icao, RunwayEnd(*a), RunwayEnd(*b))


# Maintained runway-data layer. LTFM is the primary production Error Museum
# airport. Coordinates were rechecked against current public runway data on
# 2026-09-20; generic prediction code below contains no IST heading special-case.
LTFM = AirportGeometry(
    "LTFM", "IST", "Istanbul Airport", 41.274874, 28.732136, 99.0,
    (
        _rw("LTFM", ("09", 41.25369, 28.76627, 89.0), ("27", 41.25406, 28.80002, 269.0)),
        _rw("LTFM", ("16L", 41.29860, 28.70920, 174.0), ("34R", 41.26490, 28.70990, 354.0)),
        _rw("LTFM", ("16R", 41.29860, 28.70680, 174.0), ("34L", 41.26484, 28.70741, 354.0)),
        _rw("LTFM", ("17L", 41.29880, 28.72700, 174.0), ("35R", 41.26190, 28.72780, 354.0)),
        _rw("LTFM", ("17R", 41.29880, 28.72450, 174.0), ("35L", 41.26190, 28.72530, 354.0)),
        _rw("LTFM", ("18", 41.28980, 28.75620, 179.0), ("36", 41.26220, 28.75670, 354.0)),
    ),
)

_airports: "OrderedDict[str, AirportGeometry]" = OrderedDict({"LTFM": LTFM})
_histories: "OrderedDict[str, deque[TerminalSample]]" = OrderedDict()
_evidence: dict[str, deque[RunwayEvidence]] = {}
_clusters: dict[str, "OrderedDict[str, MovementCluster]"] = {}
_last_runway: dict[str, str] = {}
_diagnostics: "OrderedDict[int, tuple[float, dict[str, Any]]]" = OrderedDict()
_pending_snapshot: tuple[tuple[Any, ...], float] | None = None
_snapshot_task: asyncio.Task | None = None


def _angle(target: float, current: float) -> float:
    return (target - current + 180.0) % 360.0 - 180.0


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def register_airport(airport: AirportGeometry) -> AirportGeometry:
    key = (airport.icao or airport.iata or airport.name).upper().strip()
    old = _airports.get(key)
    if old and old.runways and not airport.runways:
        return old
    _airports[key] = airport
    _airports.move_to_end(key)
    while len(_airports) > 128:
        first = next(iter(_airports))
        if first == "LTFM":
            _airports.move_to_end(first)
        else:
            _airports.pop(first, None)
    return airport


def airport_for_info(info: Any | None) -> AirportGeometry | None:
    if info is None:
        return None
    icao = str(getattr(info, "icao", "") or "").upper().strip()
    iata = str(getattr(info, "iata", "") or "").upper().strip()
    for airport in _airports.values():
        if (icao and airport.icao == icao) or (iata and airport.iata == iata):
            return airport
    lat, lon = _finite(getattr(info, "latitude", None)), _finite(getattr(info, "longitude", None))
    if lat is None or lon is None:
        return None
    return register_airport(AirportGeometry(icao, iata, str(getattr(info, "name", "") or icao or iata or "Airport"), lat, lon))


def nearest_airport(lat: float, lon: float, *, max_distance_km: float = 120.0) -> AirportGeometry | None:
    ranked = [(traj.haversine_km(lat, lon, a.latitude, a.longitude), a) for a in tuple(_airports.values())]
    if not ranked:
        return None
    distance, airport = min(ranked, key=lambda row: row[0])
    return airport if distance <= max_distance_km else None


def _sample(ac: Any, now: float) -> TerminalSample | None:
    lat, lon = _finite(getattr(ac, "latitude", None)), _finite(getattr(ac, "longitude", None))
    if lat is None or lon is None:
        return None
    age = max(0.0, _finite(getattr(ac, "position_age_s", 0.0)) or 0.0)
    observed = _finite(getattr(ac, "observed_at", None)) or _finite(getattr(ac, "timestamp", None)) or now - age
    age = max(age, max(0.0, now - observed))
    speed = _finite(getattr(ac, "ground_speed", None))
    if speed is None:
        velocity = _finite(getattr(ac, "velocity", None))
        speed = None if velocity is None else velocity * 1.9438444924406
    return TerminalSample(observed, lat, lon, _finite(getattr(ac, "altitude", None)), speed,
                          _finite(getattr(ac, "heading", None)), _finite(getattr(ac, "vertical_rate_mps", None)), age)


def _as_history(samples: Sequence[TerminalSample]) -> list[traj.HistorySample]:
    return [traj.HistorySample(s.timestamp, s.latitude, s.longitude, s.altitude_m, s.speed_kts,
                               s.heading_deg, s.vertical_rate_mps, s.position_age_s) for s in samples]


def _add_history(icao: str, sample: TerminalSample, now: float) -> None:
    key = icao.lower().strip()
    if not key:
        return
    if key not in _histories:
        if len(_histories) >= _HISTORY_MAX_AIRCRAFT:
            _histories.popitem(last=False)
        _histories[key] = deque(maxlen=_HISTORY_MAX_SAMPLES)
    else:
        _histories.move_to_end(key)
    q = _histories[key]
    if q and sample.timestamp <= q[-1].timestamp + .15:
        return
    if q and not traj.TrajectoryHistoryStore.transition_is_plausible(_as_history([q[-1]])[0], _as_history([sample])[0]):
        return
    q.append(sample)
    cutoff = now - _HISTORY_TTL_S
    while q and q[0].timestamp < cutoff:
        q.popleft()


def history_for(icao24: str) -> list[TerminalSample]:
    return list(_histories.get((icao24 or "").lower().strip(), ()))


def _best_end(sample: TerminalSample, airport: AirportGeometry, preferred_family: str = "") -> tuple[RunwayEnd | None, float, float]:
    if sample.heading_deg is None:
        return None, math.inf, math.inf
    ends = [e for e in airport.directed_ends if not preferred_family or e.family == preferred_family] or list(airport.directed_ends)
    best: tuple[float, float, RunwayEnd] | None = None
    for end in ends:
        distance = traj.haversine_km(sample.latitude, sample.longitude, end.latitude, end.longitude)
        h = abs(_angle(end.heading_deg, sample.heading_deg))
        bearing = traj.bearing_deg(sample.latitude, sample.longitude, end.latitude, end.longitude)
        intercept = abs(_angle(bearing, sample.heading_deg))
        score = .65 * h + .35 * intercept + .4 * max(0.0, distance - 35.0)
        row = (score, distance, end)
        if best is None or row[:2] < best[:2]:
            best = row
    return (best[2], best[0], best[1]) if best else (None, math.inf, math.inf)


def _runway_evidence(icao: str, sample: TerminalSample, airport: AirportGeometry) -> RunwayEvidence | None:
    end, score, distance = _best_end(sample, airport)
    if end is None or distance > 35 or sample.heading_deg is None:
        return None
    heading_error = abs(_angle(end.heading_deg, sample.heading_deg))
    bearing_error = abs(_angle(traj.bearing_deg(sample.latitude, sample.longitude, end.latitude, end.longitude), sample.heading_deg))
    agl = None if sample.altitude_m is None else sample.altitude_m - airport.elevation_m
    low = agl is not None and agl <= 2600
    if low and heading_error <= 16 and bearing_error <= 20 and (sample.vertical_rate_mps or 0) <= -.7:
        return RunwayEvidence(sample.timestamp, icao, "arrival", end.identifier, end.family, max(.2, 1 - score / 45))
    if low and distance <= 15 and heading_error <= 18 and (sample.vertical_rate_mps or 0) >= 1.2:
        return RunwayEvidence(sample.timestamp, icao, "departure", end.identifier, end.family, max(.2, 1 - (heading_error + distance) / 45))
    return None


def _record_evidence(airport: AirportGeometry, item: RunwayEvidence, now: float) -> None:
    q = _evidence.setdefault(airport.icao, deque(maxlen=_MAX_EVIDENCE))
    if any(old.icao24 == item.icao24 and old.movement == item.movement and now - old.timestamp <= 180 for old in reversed(q)):
        return
    q.append(item)
    while q and q[0].timestamp < now - _EVIDENCE_TTL_S:
        q.popleft()


def infer_runway_configuration(airport: AirportGeometry, *, now: float | None = None) -> RunwayInference:
    now = time.time() if now is None else float(now)
    items = [e for e in _evidence.get(airport.icao, ()) if now - e.timestamp <= _EVIDENCE_TTL_S]
    if not items:
        return RunwayInference(airport.icao)
    groups: dict[str, dict[str, Any]] = {}
    all_aircraft: set[str] = set()
    for item in items:
        all_aircraft.add(item.icao24)
        row = groups.setdefault(item.runway_family, {"aircraft": set(), "weight": 0.0, "arrivals": 0, "departures": 0, "ids": {}})
        row["aircraft"].add(item.icao24)
        w = item.score * .5 ** ((now - item.timestamp) / 600) * (1.0 if item.movement == "arrival" else .55)
        row["weight"] += w
        row[item.movement + "s"] += 1
        row["ids"][item.runway_id] = row["ids"].get(item.runway_id, 0.0) + w
    family, best = max(groups.items(), key=lambda pair: (pair[1]["weight"], len(pair[1]["aircraft"])))
    support = len(best["aircraft"])
    dominance = best["weight"] / (sum(row["weight"] for row in groups.values()) or 1.0)
    if support < 2 or len(all_aircraft) < 2 or dominance < .52:
        return RunwayInference(airport.icao, total_aircraft=len(all_aircraft))
    confidence = "High" if support >= 6 and dominance >= .68 else "Medium" if support >= 3 and dominance >= .58 else "Low"
    runway_id = max(best["ids"], key=best["ids"].get)
    previous = _last_runway.get(airport.icao)
    changed = bool(previous and previous != family and support >= 3)
    if confidence in {"Medium", "High"}:
        _last_runway[airport.icao] = family
    return RunwayInference(airport.icao, family, runway_id, confidence, support, len(all_aircraft),
                           best["arrivals"], best["departures"], round(dominance, 4), changed,
                           max(0.0, now - max(e.timestamp for e in items)))


def _vector_state(samples: Sequence[TerminalSample], now: float) -> str:
    recent = list(samples)[-10:]
    if len([s for s in recent if s.heading_deg is not None]) < 4:
        return "INSUFFICIENT"
    evidence = turn_evidence(_as_history(recent), now=now)
    if evidence.stable:
        return "SUSTAINED_TURN"
    headings = [s.heading_deg for s in recent if s.heading_deg is not None]
    signs: list[int] = []
    for a, b in zip(recent, recent[1:]):
        if a.heading_deg is None or b.heading_deg is None or not .75 <= b.timestamp - a.timestamp <= 22:
            continue
        delta = _angle(b.heading_deg, a.heading_deg)
        if abs(delta / (b.timestamp - a.timestamp)) >= .08:
            signs.append(1 if delta > 0 else -1)
    if len(signs) >= 4 and sum(a != b for a, b in zip(signs, signs[1:])) >= 2:
        return "REPEATED_VECTOR_CHANGES"
    if abs(_angle(headings[-1], headings[0])) >= 7 and abs(_angle(headings[-1], headings[-2])) <= 3:
        return "SUSTAINED_VECTOR"
    reference = headings[-1]
    if max(abs(_angle(h, reference)) for h in headings) <= 4:
        return "STABLE_STRAIGHT"
    return "VARIABLE_TRACK"


def _holding(samples: Sequence[TerminalSample]) -> tuple[bool, float]:
    if len(samples) < 12 or samples[-1].timestamp - samples[0].timestamp < 180:
        return False, 0.0
    usable = [s for s in samples if s.heading_deg is not None]
    if len(usable) < 10:
        return False, 0.0
    turn = sum(abs(_angle(b.heading_deg, a.heading_deg)) for a, b in zip(usable, usable[1:]))
    old = [s for s in samples[:-4] if samples[-1].timestamp - s.timestamp >= 120]
    repeat = min((traj.haversine_km(s.latitude, s.longitude, samples[-1].latitude, samples[-1].longitude) for s in old), default=math.inf)
    alts = [s.altitude_m for s in samples if s.altitude_m is not None]
    band = max(alts) - min(alts) if len(alts) >= 6 else math.inf
    score = (.2 if samples[-1].timestamp - samples[0].timestamp >= 240 else 0) + (.35 if turn >= 420 else .2 if turn >= 300 else 0) + (.3 if repeat <= 12 else .15 if repeat <= 20 else 0) + (.15 if band <= 600 else 0)
    return score >= .75, min(1.0, score)


def _alignment(sample: TerminalSample, end: RunwayEnd) -> tuple[float, float, float]:
    h = math.inf if sample.heading_deg is None else abs(_angle(end.heading_deg, sample.heading_deg))
    d = traj.haversine_km(sample.latitude, sample.longitude, end.latitude, end.longitude)
    if sample.heading_deg is None:
        return h, math.inf, d
    return h, abs(_angle(traj.bearing_deg(sample.latitude, sample.longitude, end.latitude, end.longitude), sample.heading_deg)), d


def _terminal_state(samples: Sequence[TerminalSample], airport: AirportGeometry, end: RunwayEnd | None, now: float) -> str:
    if end is None or len(samples) < 4:
        return ""
    recent = list(samples)[-6:]
    rows = [_alignment(s, end) for s in recent]
    h, intercept, distance = rows[-1]
    descending = sum((s.vertical_rate_mps or 0) <= -.5 for s in recent[-4:])
    if sum(rh <= 10 and ri <= 16 for rh, ri, _ in rows[-4:]) >= 3 and distance <= 28 and descending >= 2:
        return "ESTABLISHED_FINAL"
    earlier = median([rh for rh, _, _ in rows[:-1] if math.isfinite(rh)]) if len(rows) > 1 else math.inf
    if h <= 14 and intercept <= 22 and distance <= 40 and earlier >= 16:
        return "FINAL_INTERCEPT"
    turn = turn_evidence(_as_history(recent), now=now)
    if turn.stable and distance <= 50 and 14 < h <= 70 and descending >= 2 and rows[0][0] >= h + 4:
        first = recent[0].heading_deg
        reciprocal = (end.heading_deg + 180) % 360
        return "DOWNWIND_TO_BASE" if first is not None and abs(_angle(reciprocal, first)) <= 35 else "BASE_TURN"
    return ""


def _go_around(samples: Sequence[TerminalSample], airport: AirportGeometry, end: RunwayEnd | None) -> tuple[bool, bool]:
    if end is None or len(samples) < 6:
        return False, False
    recent = list(samples)[-18:]
    prior = [s for s in recent[:-2] if _alignment(s, end)[0] <= 12 and _alignment(s, end)[1] <= 20 and _alignment(s, end)[2] <= 25 and s.altitude_m is not None and s.altitude_m - airport.elevation_m <= 1800]
    if len(prior) < 2:
        return False, False
    latest, previous = recent[-1], recent[-2]
    gain = latest.altitude_m is not None and prior[-1].altitude_m is not None and latest.altitude_m - prior[-1].altitude_m >= 60
    climb = (latest.vertical_rate_mps or 0) >= 2 and ((previous.vertical_rate_mps or 0) >= 1 or gain)
    h, _, distance = _alignment(latest, end)
    go = bool(climb and (h >= 10 or distance <= 12))
    minimum = min(_alignment(s, end)[2] for s in prior)
    missed = bool(go and minimum <= 8 and (distance > minimum + 1 or h >= 18))
    return go, missed


def _segment_cpa(a: tuple[float, float], b: tuple[float, float], observer: tuple[float, float]) -> float:
    return min(traj.haversine_km(a[0] + (b[0] - a[0]) * i / 24, a[1] + (b[1] - a[1]) * i / 24, *observer) for i in range(25))


def runway_continuation_cpa_km(sample: TerminalSample, end: RunwayEnd, observer_lat: float, observer_lon: float) -> float:
    beyond = traj.project_point(end.latitude, end.longitude, 18, end.heading_deg)
    observer = (observer_lat, observer_lon)
    return min(_segment_cpa((sample.latitude, sample.longitude), (end.latitude, end.longitude), observer),
               _segment_cpa((end.latitude, end.longitude), beyond, observer))


def _cluster_label(icao: str, sample: TerminalSample, airport: AirportGeometry, evidence: RunwayEvidence | None) -> tuple[str, str, str]:
    hist = history_for(icao)
    holding, _ = _holding(hist)
    vector = _vector_state(hist, sample.timestamp) if len(hist) >= 4 else "INSUFFICIENT"
    distance = traj.haversine_km(sample.latitude, sample.longitude, airport.latitude, airport.longitude)
    agl = None if sample.altitude_m is None else sample.altitude_m - airport.elevation_m
    if holding:
        return "probable-hold", "hold", ""
    if evidence:
        return f"{evidence.movement}-{evidence.runway_family}", evidence.movement, evidence.runway_family
    if vector in {"SUSTAINED_VECTOR", "REPEATED_VECTOR_CHANGES"} and distance <= 70:
        return "vectoring", "vectoring", ""
    if (sample.vertical_rate_mps or 0) <= -.7 and distance <= 80:
        return "arrival-unassigned", "arrival", ""
    if (sample.vertical_rate_mps or 0) >= 1.2 and distance <= 50:
        return "departure-unassigned", "departure", ""
    if agl is None or agl >= 4500:
        return "overhead-transit", "transit", ""
    return "terminal-other", "other", ""


def _update_cluster(icao: str, sample: TerminalSample, airport: AirportGeometry, evidence: RunwayEvidence | None, now: float) -> None:
    label, movement, family = _cluster_label(icao, sample, airport, evidence)
    clusters = _clusters.setdefault(airport.icao, OrderedDict())
    cluster = clusters.get(label)
    if cluster is None:
        if len(clusters) >= 16:
            clusters.popitem(last=False)
        cluster = MovementCluster(label, movement, family, now, now)
        clusters[label] = cluster
    cluster.last_seen = now
    clusters.move_to_end(label)
    if now - cluster.aircraft_last_seen.get(icao, -math.inf) >= 180:
        cluster.sample_count += 1
        cluster.aircraft_last_seen[icao] = now
    for key, seen in list(cluster.aircraft_last_seen.items()):
        if seen < now - 2700:
            cluster.aircraft_last_seen.pop(key, None)


def recent_movement_clusters(airport: AirportGeometry | None, *, now: float | None = None) -> tuple[dict[str, Any], ...]:
    if airport is None:
        return ()
    now = time.time() if now is None else float(now)
    clusters = _clusters.get(airport.icao, OrderedDict())
    for label in list(clusters):
        if clusters[label].last_seen < now - 2700:
            clusters.pop(label, None)
    rows = [{"label": c.label, "movement": c.movement, "runway_family": c.runway_family,
             "sample_count": c.sample_count, "recent_aircraft": len(c.aircraft_last_seen),
             "age_s": max(0.0, now - c.last_seen),
             "decayed_support": round(c.sample_count * .5 ** (max(0.0, now - c.last_seen) / 900), 3)} for c in clusters.values()]
    return tuple(sorted(rows, key=lambda r: (r["decayed_support"], r["sample_count"]), reverse=True))


def _ingest(aircraft: Sequence[Any], now: float) -> None:
    for ac in aircraft:
        icao = str(getattr(ac, "icao24", "") or "").lower().strip()
        sample = _sample(ac, now)
        if not icao or sample is None or sample.position_age_s > 30:
            continue
        _add_history(icao, sample, now)
        for airport in tuple(_airports.values()):
            if not airport.runways or traj.haversine_km(sample.latitude, sample.longitude, airport.latitude, airport.longitude) > 55:
                continue
            evidence = _runway_evidence(icao, sample, airport)
            if evidence:
                _record_evidence(airport, evidence, now)
            _update_cluster(icao, sample, airport, evidence, now)
    cutoff = now - _HISTORY_TTL_S
    for key in list(_histories):
        while _histories[key] and _histories[key][0].timestamp < cutoff:
            _histories[key].popleft()
        if not _histories[key]:
            _histories.pop(key, None)


def enqueue_provider_snapshot(aircraft: Sequence[Any], *, now: float | None = None) -> None:
    global _pending_snapshot, _snapshot_task
    _pending_snapshot = (tuple(aircraft[:_MAX_SNAPSHOT]), time.time() if now is None else float(now))
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        snapshot, ts = _pending_snapshot
        _pending_snapshot = None
        _ingest(snapshot, ts)
        return
    if _snapshot_task is None or _snapshot_task.done():
        _snapshot_task = loop.create_task(_drain_snapshots(), name="airport-terminal-v47")


async def _drain_snapshots() -> None:
    global _pending_snapshot
    while _pending_snapshot is not None:
        snapshot, ts = _pending_snapshot
        _pending_snapshot = None
        _ingest(snapshot, ts)
        await asyncio.sleep(0)


def assess_terminal(samples: Sequence[TerminalSample], airport: AirportGeometry | None, *, now: float | None = None,
                    observer_lat: float | None = None, observer_lon: float | None = None) -> TerminalAssessment:
    now = time.time() if now is None else float(now)
    if not samples or airport is None:
        return TerminalAssessment("", "", False, math.inf, "OUTSIDE_TERMINAL", "Uncertain", 0, "INSUFFICIENT", "", "", "Uncertain", 0, False, 0, False, False, None, ("no airport context",))
    samples = sorted(samples, key=lambda s: s.timestamp)
    latest = samples[-1]
    distance = traj.haversine_km(latest.latitude, latest.longitude, airport.latitude, airport.longitude)
    inference = infer_runway_configuration(airport, now=now) if airport.runways else RunwayInference(airport.icao)
    end, _, _ = _best_end(latest, airport, inference.runway_family) if airport.runways else (None, math.inf, math.inf)
    vector = _vector_state(samples, now)
    holding, holding_score = _holding(samples)
    runway_state = _terminal_state(samples, airport, end, now)
    go, missed = _go_around(samples, airport, end)
    agl = None if latest.altitude_m is None else latest.altitude_m - airport.elevation_m
    terminal = distance <= 55 or (distance <= 90 and agl is not None and agl <= 6500 and ((latest.vertical_rate_mps or 0) <= -.5 or (latest.speed_kts or 999) <= 340))
    state = "TERMINAL_UNCERTAIN" if terminal else "OUTSIDE_TERMINAL"
    reasons: list[str] = [f"within {distance:.1f} km of {airport.icao or airport.iata}"] if terminal else []
    if terminal and holding:
        state = "PROBABLE_HOLDING"; reasons.append("repeated looping path with stable altitude band")
    if terminal and runway_state:
        state = runway_state; reasons.append("sustained runway-relative motion")
    if go:
        state = "GO_AROUND"; reasons.append("final alignment changed to sustained climb/deviation")
    if missed:
        state = "MISSED_APPROACH"; reasons.append("climb/deviation after close threshold geometry")
    elif terminal and vector in {"SUSTAINED_VECTOR", "REPEATED_VECTOR_CHANGES"} and not holding and not runway_state:
        state = vector; reasons.append("sustained course-change evidence")
    penalty = 0.0 if not terminal else .16 if state == "PROBABLE_HOLDING" else .18 if state in {"GO_AROUND", "MISSED_APPROACH"} else .10 if state in {"BASE_TURN", "DOWNWIND_TO_BASE", "FINAL_INTERCEPT", "SUSTAINED_VECTOR", "REPEATED_VECTOR_CHANGES"} else .06
    confidence = "High" if state in {"ESTABLISHED_FINAL", "GO_AROUND", "MISSED_APPROACH"} else "Medium" if state in {"BASE_TURN", "DOWNWIND_TO_BASE", "FINAL_INTERCEPT", "PROBABLE_HOLDING"} else "Low" if terminal else "Uncertain"
    runway_cpa = runway_continuation_cpa_km(latest, end, observer_lat, observer_lon) if end and observer_lat is not None and observer_lon is not None else None
    return TerminalAssessment(airport.icao, airport.iata, terminal, distance, state, confidence, penalty, vector,
                              end.identifier if end else inference.runway_id, end.family if end else inference.runway_family,
                              inference.confidence, inference.supporting_aircraft, holding, holding_score, go, missed,
                              runway_cpa, tuple(reasons))


def associate_historical_runways(paths: Iterable[Iterable[Any]], airport: AirportGeometry | None) -> tuple[str, int, int]:
    if airport is None or not airport.runways:
        return "", 0, 0
    counts: dict[str, int] = {}; total = 0
    for raw in paths:
        points = list(raw)
        if len(points) < 3:
            continue
        try:
            a, b = points[-2], points[-1]
            heading = traj.bearing_deg(float(a.latitude), float(a.longitude), float(b.latitude), float(b.longitude))
            sample = TerminalSample(0, float(b.latitude), float(b.longitude), None, None, heading, None)
        except (AttributeError, TypeError, ValueError):
            continue
        end, score, distance = _best_end(sample, airport)
        if end and distance <= 40 and score <= 35:
            counts[end.family] = counts.get(end.family, 0) + 1; total += 1
    if not counts:
        return "", 0, total
    family, support = max(counts.items(), key=lambda row: (row[1], row[0]))
    return family, support, total


def shadow_terminal_hold(assessment: TerminalAssessment, *, pred: Any, alert_radius_km: float) -> tuple[bool, str]:
    if bool(getattr(pred, "stale", False)):
        return False, "stale observation; no airport shadow decision"
    current = float(getattr(pred, "current_distance_km", math.inf))
    if current <= alert_radius_km:
        return False, "fresh physical radius entry remains authoritative"
    if assessment.go_around or assessment.missed_approach:
        return False, "go-around/missed-approach requires live re-evaluation"
    if assessment.probable_holding and float(getattr(pred, "time_to_cpa_s", 9999) or 9999) > 60:
        return True, "probable holding pattern makes early ETA/CPA unstable"
    if assessment.state in {"DOWNWIND_TO_BASE", "BASE_TURN", "FINAL_INTERCEPT", "ESTABLISHED_FINAL"}:
        margin = alert_radius_km + max(2.0, alert_radius_km * .15)
        if assessment.runway_path_cpa_km is not None and assessment.runway_path_cpa_km > margin:
            return True, f"runway-aligned continuation misses observer ({assessment.runway_path_cpa_km:.1f} km)"
    return False, "airport context does not justify a shadow hold"


def record_prediction_diagnostics(prediction: Any, payload: dict[str, Any]) -> None:
    now = time.monotonic(); _diagnostics[id(prediction)] = (now, dict(payload)); _diagnostics.move_to_end(id(prediction))
    for key, (seen, _) in list(_diagnostics.items()):
        if seen >= now - _DIAG_TTL_S:
            break
        _diagnostics.pop(key, None)
    while len(_diagnostics) > _DIAG_MAX:
        _diagnostics.popitem(last=False)


def diagnostics_for(prediction: Any) -> dict[str, Any]:
    entry = _diagnostics.get(id(prediction))
    if not entry or time.monotonic() - entry[0] > _DIAG_TTL_S:
        _diagnostics.pop(id(prediction), None); return {}
    return dict(entry[1])


def diagnostic_payload(assessment: TerminalAssessment, inference: RunwayInference, *, shadow_hold: bool, shadow_reason: str,
                       route_cluster_runway: str = "", route_cluster_support: int = 0, route_cluster_total: int = 0,
                       authoritative_override: str = "") -> dict[str, Any]:
    return {
        "model_version": AIRPORT_SHADOW_MODEL_VERSION, "shadow_only": True,
        "airport": assessment.airport_icao or assessment.airport_iata, "terminal_area": assessment.terminal_area,
        "terminal_state": assessment.state, "terminal_state_confidence": assessment.state_confidence,
        "terminal_confidence_penalty": assessment.confidence_penalty, "vector_state": assessment.vector_state,
        "runway_candidate": assessment.runway_id, "runway_family": assessment.runway_family,
        "runway_confidence": inference.confidence, "runway_supporting_aircraft": inference.supporting_aircraft,
        "runway_total_aircraft": inference.total_aircraft, "runway_changed": inference.changed,
        "probable_holding": assessment.probable_holding, "holding_score": round(assessment.holding_score, 4),
        "go_around": assessment.go_around, "missed_approach": assessment.missed_approach,
        "runway_path_cpa_km": assessment.runway_path_cpa_km, "route_cluster_runway": route_cluster_runway,
        "route_cluster_support": route_cluster_support, "route_cluster_total": route_cluster_total,
        "shadow_hold": shadow_hold, "shadow_reason": shadow_reason, "authoritative_override": authoritative_override,
        "reasons": list(assessment.reasons),
    }


def reset_v47_state_for_tests() -> None:
    global _pending_snapshot, _snapshot_task
    _histories.clear(); _evidence.clear(); _clusters.clear(); _last_runway.clear(); _diagnostics.clear(); _pending_snapshot = None
    if _snapshot_task is not None and not _snapshot_task.done():
        _snapshot_task.cancel()
    _snapshot_task = None; _airports.clear(); _airports["LTFM"] = LTFM
