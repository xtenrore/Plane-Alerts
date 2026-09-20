"""Plane? deterministic trajectory / CPA intelligence.

The predictor deliberately favours stable observed motion over single-feed spikes.
Destination/airport data is never used to decide whether an aircraft approaches
an observer.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from statistics import median
from typing import Iterable

EARTH_RADIUS_KM = 6371.0088
KNOTS_TO_KM_S = 0.0005144444444444444
_MAX_CONTIGUOUS_GAP_S = 22.0
_RECENT_MODEL_WINDOW_S = 75.0
_TURN_DECAY_S = 45.0
_ACCEL_DECAY_S = 20.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _angle_delta(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project_point(lat: float, lon: float, distance_km: float, bearing: float) -> tuple[float, float]:
    if distance_km == 0:
        return lat, lon
    p1 = math.radians(lat)
    l1 = math.radians(lon)
    b = math.radians(bearing)
    d = distance_km / EARTH_RADIUS_KM
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


@dataclass(slots=True)
class HistorySample:
    timestamp: float
    latitude: float
    longitude: float
    altitude_m: float | None = None
    speed_kts: float | None = None
    heading_deg: float | None = None
    vertical_rate_mps: float | None = None
    position_age_s: float = 0.0


@dataclass(slots=True)
class ProjectedPoint:
    seconds: float
    latitude: float
    longitude: float
    horizontal_km: float
    slant_km: float
    altitude_m: float | None
    heading_deg: float


@dataclass(slots=True)
class TrajectoryPrediction:
    state: str
    confidence: str
    confidence_score: float
    current_distance_km: float
    current_slant_km: float | None
    distance_trend_km_s: float | None
    projected_closest_km: float
    projected_closest_slant_km: float | None
    time_to_cpa_s: float | None
    radius_entry_s: float | None
    enters_alert_radius: bool
    already_passed: bool
    turning_away: bool
    stale: bool
    turn_rate_deg_s: float
    acceleration_kts_s: float
    heading_stability_deg: float | None
    speed_stability_kts: float | None
    reason: str
    path: list[ProjectedPoint] = field(default_factory=list)


class TrajectoryHistoryStore:
    """Bounded rolling history with a generous physical outlier gate.

    Multiple public ADS-B feeds can briefly disagree on the same ICAO24. A bad
    single point must never become the seed for a completely different CPA.
    """
    def __init__(self, max_age_s: float = 120.0, max_samples: int = 64, max_aircraft: int = 8192) -> None:
        self.max_age_s = max_age_s
        self.max_samples = max_samples
        self.max_aircraft = max_aircraft
        self._data: dict[str, deque[HistorySample]] = defaultdict(lambda: deque(maxlen=max_samples))

    @staticmethod
    def transition_is_plausible(previous: HistorySample, sample: HistorySample) -> bool:
        dt = sample.timestamp - previous.timestamp
        if dt < -0.25:
            return False
        if dt <= 0.25:
            return haversine_km(previous.latitude, previous.longitude, sample.latitude, sample.longitude) <= 1.25
        distance = haversine_km(previous.latitude, previous.longitude, sample.latitude, sample.longitude)
        return distance <= 0.8 + 0.70 * dt

    def add(self, icao24: str, sample: HistorySample) -> list[HistorySample]:
        key = (icao24 or "").lower().strip()
        if not key:
            return [sample]
        if key not in self._data and len(self._data) >= self.max_aircraft:
            self._data.pop(next(iter(self._data)))
        q = self._data[key]
        if q and not self.transition_is_plausible(q[-1], sample):
            return list(q)
        if not q or sample.timestamp > q[-1].timestamp + 0.15:
            q.append(sample)
        elif sample.timestamp >= q[-1].timestamp:
            q[-1] = sample
        cutoff = sample.timestamp - self.max_age_s
        while q and q[0].timestamp < cutoff:
            q.popleft()
        return list(q)

    def get(self, icao24: str) -> list[HistorySample]:
        return list(self._data.get((icao24 or "").lower().strip(), ()))

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        cutoff = now - self.max_age_s * 2
        for key in list(self._data):
            q = self._data[key]
            while q and q[0].timestamp < cutoff:
                q.popleft()
            if not q:
                self._data.pop(key, None)


def _contiguous_recent(samples: list[HistorySample]) -> list[HistorySample]:
    if not samples:
        return []
    latest_t = samples[-1].timestamp
    kept = [samples[-1]]
    for item in reversed(samples[:-1]):
        if kept[0].timestamp - item.timestamp > _MAX_CONTIGUOUS_GAP_S:
            break
        if latest_t - item.timestamp > _RECENT_MODEL_WINDOW_S:
            break
        if not TrajectoryHistoryStore.transition_is_plausible(item, kept[0]):
            break
        kept.insert(0, item)
    return kept


def _robust_angle(values: list[float]) -> float | None:
    if not values:
        return None
    ref = values[-1] % 360.0
    unwrapped = [ref + _angle_delta(v % 360.0, ref) for v in values]
    return median(unwrapped) % 360.0


def _series_heading(samples: list[HistorySample]) -> list[float]:
    values = [float(s.heading_deg) % 360.0 for s in samples if s.heading_deg is not None]
    if len(values) >= 2:
        return values
    out: list[float] = []
    for a, b in zip(samples, samples[1:]):
        if haversine_km(a.latitude, a.longitude, b.latitude, b.longitude) > 0.02:
            out.append(bearing_deg(a.latitude, a.longitude, b.latitude, b.longitude))
    return out


def _heading_spread(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    ref = _robust_angle(values)
    assert ref is not None
    diffs = [_angle_delta(v, ref) for v in values]
    mean = sum(diffs) / len(diffs)
    return math.sqrt(sum((d - mean) ** 2 for d in diffs) / len(diffs))


def _turn_rate(samples: list[HistorySample]) -> float:
    rates: list[float] = []
    usable = [s for s in samples if s.heading_deg is not None]
    for a, b in zip(usable, usable[1:]):
        dt = b.timestamp - a.timestamp
        if 0.75 <= dt <= 20:
            r = _angle_delta(float(b.heading_deg), float(a.heading_deg)) / dt
            if abs(r) <= 6.0:
                rates.append(r)
    if not rates:
        return 0.0
    meaningful = [r for r in rates[-7:] if abs(r) >= 0.04]
    if len(meaningful) >= 3:
        positive = sum(r > 0 for r in meaningful)
        negative = len(meaningful) - positive
        if max(positive, negative) / len(meaningful) < 0.67:
            return 0.0
    return _clamp(median(rates[-7:]), -2.5, 2.5)


def _acceleration(samples: list[HistorySample]) -> float:
    vals = [s for s in samples if s.speed_kts is not None]
    rates: list[float] = []
    for a, b in zip(vals, vals[1:]):
        dt = b.timestamp - a.timestamp
        if 0.75 <= dt <= 20:
            rate = (float(b.speed_kts) - float(a.speed_kts)) / dt
            if abs(rate) <= 4.0:
                rates.append(rate)
    if not rates:
        return 0.0
    return _clamp(median(rates[-5:]), -1.5, 1.5)


def _speed_spread(samples: list[HistorySample]) -> float | None:
    v = [float(s.speed_kts) for s in samples if s.speed_kts is not None]
    if len(v) < 2:
        return None
    m = sum(v) / len(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / len(v))


def _distance_trend(samples: list[HistorySample], user_lat: float, user_lon: float) -> float | None:
    if len(samples) < 2:
        return None
    recent = samples[-min(7, len(samples)):]
    pairs: list[float] = []
    for a, b in zip(recent, recent[1:]):
        dt = b.timestamp - a.timestamp
        if dt > 0.5:
            da = haversine_km(a.latitude, a.longitude, user_lat, user_lon)
            db = haversine_km(b.latitude, b.longitude, user_lat, user_lon)
            rate = (db - da) / dt
            if abs(rate) <= 0.8:
                pairs.append(rate)
    return median(pairs) if pairs else None


def _motion_heading(samples: list[HistorySample]) -> float | None:
    if len(samples) < 2:
        return None
    latest = samples[-1]
    for older in samples:
        dt = latest.timestamp - older.timestamp
        distance = haversine_km(older.latitude, older.longitude, latest.latitude, latest.longitude)
        if dt >= 3.0 and distance >= 0.12:
            return bearing_deg(older.latitude, older.longitude, latest.latitude, latest.longitude)
    return None


def _estimate_speed_heading(samples: list[HistorySample]) -> tuple[float | None, float | None]:
    latest = samples[-1]
    speed_values = [float(s.speed_kts) for s in samples[-7:] if s.speed_kts is not None and 20 <= float(s.speed_kts) <= 1400]
    speed = median(speed_values) if speed_values else latest.speed_kts
    heading_values = [float(s.heading_deg) for s in samples[-7:] if s.heading_deg is not None]
    heading = _robust_angle(heading_values)
    motion = _motion_heading(samples[-8:])

    if (speed is None or speed < 20) and len(samples) >= 2:
        a, b = samples[-2], samples[-1]
        dt = b.timestamp - a.timestamp
        dist = haversine_km(a.latitude, a.longitude, b.latitude, b.longitude)
        if dt > 0 and dist > 0.005:
            speed = dist / dt / KNOTS_TO_KM_S

    if motion is not None:
        if heading is None:
            heading = motion
        else:
            disagreement = abs(_angle_delta(heading, motion))
            if disagreement > 28.0 and len(samples) >= 3:
                heading = motion
            elif disagreement <= 28.0:
                heading = (motion + 0.35 * _angle_delta(heading, motion)) % 360.0
    return speed, heading


def predict_trajectory(samples: Iterable[HistorySample], user_lat: float, user_lon: float, alert_radius_km: float, *, now: float | None = None, user_altitude_m: float = 0.0, max_horizon_s: int = 900, step_s: int = 3) -> TrajectoryPrediction:
    raw_samples = sorted(list(samples), key=lambda s: s.timestamp)
    if not raw_samples:
        raise ValueError("at least one trajectory sample is required")
    samples = _contiguous_recent(raw_samples)
    latest = samples[-1]
    now = time.time() if now is None else now
    age = max(float(latest.position_age_s or 0.0), now - latest.timestamp)
    stale = age > 30.0
    current = haversine_km(latest.latitude, latest.longitude, user_lat, user_lon)
    cur_slant = math.hypot(current, max(0.0, latest.altitude_m - user_altitude_m) / 1000.0) if latest.altitude_m is not None else None
    speed, heading = _estimate_speed_heading(samples)
    trend = _distance_trend(samples, user_lat, user_lon)
    headings = _series_heading(samples[-12:])
    h_spread = _heading_spread(headings)
    s_spread = _speed_spread(samples[-12:])
    turn = _turn_rate(samples[-12:])
    accel = _acceleration(samples[-8:])

    if stale or speed is None or heading is None or speed < 20:
        reason = "ADS-B position is stale" if stale else "insufficient speed/heading data"
        return TrajectoryPrediction("Prediction uncertain", "Uncertain", 0.15, current, cur_slant, trend, current, cur_slant, None, None, False, False, False, stale, turn, accel, h_spread, s_spread, reason, [])

    speed_km_s = speed * KNOTS_TO_KM_S
    dynamic = int(current / max(speed_km_s, 1e-6) * 1.30 + 45)
    horizon = min(max_horizon_s, max(180, dynamic))
    vr = latest.vertical_rate_mps or 0.0
    curr_lat, curr_lon, curr_heading, altitude = latest.latitude, latest.longitude, heading % 360.0, latest.altitude_m
    projected_speed = float(speed)
    path: list[ProjectedPoint] = []
    closest_h, closest_slant, cpa_t = current, cur_slant, 0.0
    entry_t: float | None = 0.0 if current <= alert_radius_km else None

    for t in range(0, horizon + 1, step_s):
        if t > 0:
            accel_factor = max(0.0, 1.0 - (t - step_s) / _ACCEL_DECAY_S)
            projected_speed = max(20.0, projected_speed + accel * accel_factor * step_s)
            curr_lat, curr_lon = project_point(curr_lat, curr_lon, projected_speed * KNOTS_TO_KM_S * step_s, curr_heading)
            turn_factor = max(0.0, 1.0 - (t - step_s) / _TURN_DECAY_S)
            curr_heading = (curr_heading + turn * turn_factor * step_s) % 360.0
            if altitude is not None:
                altitude = max(0.0, altitude + vr * step_s)
        horizontal = haversine_km(curr_lat, curr_lon, user_lat, user_lon)
        slant = math.hypot(horizontal, max(0.0, altitude - user_altitude_m) / 1000.0) if altitude is not None else horizontal
        path.append(ProjectedPoint(float(t), curr_lat, curr_lon, horizontal, slant, altitude, curr_heading))
        if horizontal < closest_h:
            closest_h, closest_slant, cpa_t = horizontal, slant, float(t)
        if entry_t is None and horizontal <= alert_radius_km:
            entry_t = float(t)

    idx = min(range(len(path)), key=lambda i: path[i].horizontal_km)
    if 0 < idx < len(path) - 1:
        y1, y2, y3 = path[idx - 1].horizontal_km, path[idx].horizontal_km, path[idx + 1].horizontal_km
        denom = y1 - 2 * y2 + y3
        if abs(denom) > 1e-9:
            offset = _clamp(0.5 * (y1 - y3) / denom, -1.0, 1.0)
            cpa_t = max(0.0, path[idx].seconds + offset * step_s)

    increasing = trend is not None and trend > 0.002
    already_passed = cpa_t <= step_s and increasing
    horizon_edge = cpa_t >= horizon - step_s
    enters = closest_h <= alert_radius_km and cpa_t > 0 and not stale and not (horizon_edge and closest_h > alert_radius_km * 0.85)
    turning_away = abs(turn) >= 0.15 and increasing and closest_h >= min(current, alert_radius_km * 1.1)

    score = 0.30 + min(0.22, max(0, len(samples) - 1) * 0.035) + 0.16 * (1.0 - _clamp(age / 20.0, 0.0, 1.0))
    if h_spread is not None:
        score += 0.14 * (1.0 - _clamp(h_spread / 18.0, 0.0, 1.0))
    if s_spread is not None:
        score += 0.08 * (1.0 - _clamp(s_spread / 35.0, 0.0, 1.0))
    score -= 0.10 * _clamp(abs(turn), 0.0, 1.0) + 0.06 * _clamp(abs(accel) / 1.5, 0.0, 1.0) + 0.14 * _clamp(cpa_t / max_horizon_s, 0.0, 1.0)
    if len(samples) < 3:
        score -= 0.10
    if horizon_edge:
        score -= 0.10
    score = _clamp(min(score, 0.25) if stale else score, 0.0, 0.98)
    confidence = "High" if score >= 0.78 else "Medium" if score >= 0.58 else "Low" if score >= 0.36 else "Uncertain"

    if stale:
        state, reason = "Prediction uncertain", "ADS-B position is stale"
    elif turning_away:
        state, reason = "Turning away", "sustained recent turn and distance trend move the aircraft away from the observer"
    elif already_passed:
        state, reason = "Passed", "closest approach is behind the current position and distance is increasing"
    elif enters and cpa_t <= 30:
        state, reason = "Passing nearby", "robust projected path enters the configured radius and CPA is imminent"
    elif enters:
        state, reason = "Approaching", "robust projected path enters the configured radius"
    elif increasing:
        state, reason = "Moving away", f"projected closest pass remains outside {alert_radius_km:.1f} km"
    else:
        state, reason = "Will not approach", f"projected closest pass remains outside {alert_radius_km:.1f} km"

    return TrajectoryPrediction(state, confidence, score, current, cur_slant, trend, closest_h, closest_slant, cpa_t, entry_t, enters, already_passed, turning_away, stale, turn, accel, h_spread, s_spread, reason, path)
