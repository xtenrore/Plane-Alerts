"""Coverage-aware next-hour outcome evidence.

Positive presence can be proven by one measured in-radius observation. A
negative is scoreable only when the stored route observations mathematically
cover the complete expected pass window tightly enough that even a deliberately
generous aircraft-speed bound cannot hide an unobserved radius entry.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

from app.next_hour_shadow import _closest_point_in_expectation_window, _point_timestamp

OUTCOME_VERSION = "5.3-covered-negative-v1"
KNOTS_TO_KM_S = 0.0005144444444444444
# Higher than the predictor's accepted 1,400 kt ceiling. Using 1,600 kt makes
# the negative proof conservative even for bad/misreported groundspeed.
_NEGATIVE_SPEED_BOUND_KM_S = 1600.0 * KNOTS_TO_KM_S
_NEGATIVE_SAFETY_MARGIN_KM = 2.0


def _as_utc(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _route_observation(point, latitude: float, longitude: float):
    ts = _point_timestamp(point)
    if ts is None:
        return None
    try:
        plat = float(point.get("lat", point.get("latitude")))
        plon = float(point.get("lon", point.get("longitude")))
    except (AttributeError, TypeError, ValueError):
        return None
    return float(ts), _distance_km(latitude, longitude, plat, plon)


def _covered_negative(points, latitude, longitude, expectation, radius_km):
    """Return conservative continuous-coverage proof for a true negative.

    The proof requires route observations on both sides of the *stored expected
    pass window*. For every adjacent pair, triangle inequality plus a 1,600 kt
    speed ceiling gives a lower bound on the aircraft's possible distance from
    the observer anywhere between the two samples. If even one interval could
    conceal a radius entry, coverage is unresolved.
    """
    start = _as_utc(expectation.get("window_start"))
    end = _as_utc(expectation.get("window_end"))
    if start is None or end is None or end <= start:
        return {"proven": False, "sample_count": 0, "max_gap_s": None, "min_lower_bound_km": None}

    start_ts = start.timestamp()
    end_ts = end.timestamp()
    observations = sorted(
        item for point in points
        if (item := _route_observation(point, float(latitude), float(longitude))) is not None
    )
    if len(observations) < 2:
        return {"proven": False, "sample_count": len(observations), "max_gap_s": None, "min_lower_bound_km": None}

    before = [item for item in observations if item[0] <= start_ts]
    after = [item for item in observations if item[0] >= end_ts]
    if not before or not after:
        return {"proven": False, "sample_count": len(observations), "max_gap_s": None, "min_lower_bound_km": None}

    first = before[-1]
    last = after[0]
    covered = [item for item in observations if first[0] <= item[0] <= last[0]]
    max_gap = 0.0
    minimum_lower = math.inf
    threshold = float(radius_km) + _NEGATIVE_SAFETY_MARGIN_KM

    for left, right in zip(covered, covered[1:]):
        gap = right[0] - left[0]
        if gap <= 0:
            continue
        max_gap = max(max_gap, gap)
        # At every instant between two observations, one endpoint is at most
        # gap/2 seconds away. Distance therefore cannot decrease by more than
        # speed_bound * gap/2 from the nearer endpoint observation.
        lower_bound = min(left[1], right[1]) - _NEGATIVE_SPEED_BOUND_KM_S * gap / 2.0
        minimum_lower = min(minimum_lower, lower_bound)
        if lower_bound <= threshold:
            return {
                "proven": False,
                "sample_count": len(covered),
                "max_gap_s": round(max_gap, 1),
                "min_lower_bound_km": round(minimum_lower, 3),
            }

    if not math.isfinite(minimum_lower):
        return {"proven": False, "sample_count": len(covered), "max_gap_s": None, "min_lower_bound_km": None}
    return {
        "proven": True,
        "sample_count": len(covered),
        "max_gap_s": round(max_gap, 1),
        "min_lower_bound_km": round(minimum_lower, 3),
    }


def observed_outcome(points, latitude, longitude, expectation, radius_km):
    closest, count, bounds = _closest_point_in_expectation_window(points, latitude, longitude, expectation)
    result = {
        "closest": closest,
        "sample_count": count,
        "actual_pass": None,
        "scoreable": False,
        "timing_scoreable": False,
        "outcome_version": OUTCOME_VERSION,
        "negative_coverage_proven": False,
        "negative_coverage_sample_count": 0,
        "negative_coverage_max_gap_s": None,
        "negative_coverage_min_lower_bound_km": None,
    }
    if closest is None or bounds is None:
        return result
    distance, timestamp = closest

    # One measured inside point is sufficient positive ground truth.
    if distance <= radius_km:
        result.update(actual_pass=True, scoreable=True)
        times = sorted(set(ts for point in points if (ts := _point_timestamp(point)) is not None
                           and bounds[0].timestamp() <= ts <= bounds[1].timestamp()))
        index = times.index(timestamp)
        # Report sampled CPA timing only when close observations bracket the
        # minimum; an isolated or boundary point is not a measured closest pass.
        result["timing_scoreable"] = (
            0 < index < len(times) - 1
            and timestamp - times[index - 1] <= 30
            and times[index + 1] - timestamp <= 30
        )
        return result

    # Outside observations become a true negative only with complete,
    # physically bounded coverage. Sparse observations remain unresolved.
    proof = _covered_negative(points, latitude, longitude, expectation, radius_km)
    result.update(
        negative_coverage_proven=bool(proof["proven"]),
        negative_coverage_sample_count=int(proof["sample_count"]),
        negative_coverage_max_gap_s=proof["max_gap_s"],
        negative_coverage_min_lower_bound_km=proof["min_lower_bound_km"],
    )
    if proof["proven"]:
        result.update(actual_pass=False, scoreable=True)
    return result
