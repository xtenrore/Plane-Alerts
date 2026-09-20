"""Coverage-aware outcome evidence. An absent observation is never a miss."""
from __future__ import annotations

from app.next_hour_shadow import _closest_point_in_expectation_window, _point_timestamp

OUTCOME_VERSION = "4.4-bounded-observed-pass"


def observed_outcome(points, latitude, longitude, expectation, radius_km):
    closest, count, bounds = _closest_point_in_expectation_window(points, latitude, longitude, expectation)
    result = {"closest": closest, "sample_count": count, "actual_pass": None,
              "scoreable": False, "timing_scoreable": False, "outcome_version": OUTCOME_VERSION}
    if closest is None or bounds is None:
        return result
    distance, timestamp = closest
    # A measured inside point proves presence. Observations outside the radius
    # cannot prove absence during a coverage gap, regardless of sample count.
    if distance > radius_km:
        return result
    result.update(actual_pass=True, scoreable=True)
    times = sorted(set(ts for point in points if (ts := _point_timestamp(point)) is not None
                       and bounds[0].timestamp() <= ts <= bounds[1].timestamp()))
    index = times.index(timestamp)
    # Report sampled CPA timing only when close observations bracket the
    # minimum; an isolated or boundary point is not a measured closest pass.
    result["timing_scoreable"] = (0 < index < len(times)-1
        and timestamp-times[index-1] <= 30 and times[index+1]-timestamp <= 30)
    return result
