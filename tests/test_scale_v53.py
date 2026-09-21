from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.aircraft.models import NormalizedAircraft
from app.intelligence import trajectory as base
from app.intelligence import trajectory_scale_v53 as scaled
from app.version import PREDICTION_VERSION, VERSION
from app.worker import scale_v53


def _samples(now: float = 1_800_000_000.0, *, lat: float = 41.30, lon: float = 28.70):
    return [
        base.HistorySample(timestamp=now - 15.0, latitude=lat, longitude=lon, altitude_m=3600.0, speed_kts=260.0, heading_deg=90.0, vertical_rate_mps=-1.0, position_age_s=0.5),
        base.HistorySample(timestamp=now - 10.0, latitude=lat, longitude=lon + 0.010, altitude_m=3595.0, speed_kts=261.0, heading_deg=90.5, vertical_rate_mps=-1.0, position_age_s=0.5),
        base.HistorySample(timestamp=now - 5.0, latitude=lat, longitude=lon + 0.020, altitude_m=3590.0, speed_kts=262.0, heading_deg=91.0, vertical_rate_mps=-1.0, position_age_s=0.5),
    ]


def _aircraft(icao: str, lat: float, lon: float) -> NormalizedAircraft:
    return NormalizedAircraft(icao24=icao, callsign=f"TEST{icao[-2:]}", latitude=lat, longitude=lon, altitude=3500.0, velocity=210.0, heading=90.0, position_age_s=1.0, aircraft_type="A320")


def _user(uid: int, lat: float, lon: float, radius: float = 15.0) -> dict:
    return {"user_id": uid, "location": {"latitude": lat, "longitude": lon, "radius_km": radius, "geohash": f"u{uid}"}, "preferences": {}}


def _assert_prediction_equivalent(left, right) -> None:
    scalar_fields = ("state", "confidence", "enters_alert_radius", "already_passed", "turning_away", "stale", "three_d_available", "observer_altitude_known", "altitude_confidence", "altitude_relevance_applied", "altitude_relevance_reason", "reason")
    for field in scalar_fields:
        assert getattr(left, field) == getattr(right, field)
    numeric_fields = ("confidence_score", "current_distance_km", "current_slant_km", "distance_trend_km_s", "projected_closest_km", "projected_closest_slant_km", "time_to_cpa_s", "radius_entry_s", "turn_rate_deg_s", "acceleration_kts_s", "heading_stability_deg", "speed_stability_kts", "current_vertical_separation_m", "projected_closest_3d_km", "projected_closest_3d_lower_bound_km", "time_to_3d_cpa_s", "horizontal_at_3d_cpa_km", "vertical_at_3d_cpa_m", "altitude_uncertainty_m")
    for field in numeric_fields:
        a = getattr(left, field)
        b = getattr(right, field)
        if a is None or b is None:
            assert a is b
        else:
            assert a == pytest.approx(b, abs=1e-9, rel=1e-9)
    assert len(left.path) == len(right.path)
    for a, b in zip(left.path, right.path):
        assert a.seconds == b.seconds
        assert a.latitude == pytest.approx(b.latitude, abs=1e-12)
        assert a.longitude == pytest.approx(b.longitude, abs=1e-12)
        assert a.horizontal_km == pytest.approx(b.horizontal_km, abs=1e-9)
        assert a.slant_km == pytest.approx(b.slant_km, abs=1e-9)
        if a.altitude_m is None or b.altitude_m is None:
            assert a.altitude_m is b.altitude_m
        else:
            assert a.altitude_m == pytest.approx(b.altitude_m, abs=1e-9)
        assert a.heading_deg == pytest.approx(b.heading_deg, abs=1e-9)


def test_v53_release_identity_preserves_age_aware_physical_predictor():
    assert tuple(int(part) for part in VERSION.split(".")) >= (5, 3, 0)
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"


def test_shared_motion_is_output_equivalent_for_multiple_observers():
    now = 1_800_000_000.0
    samples = _samples(now)
    scaled.reset_motion_cache_for_tests()
    observers = [(41.25, 28.95, 15.0, 80.0), (41.32, 29.02, 25.0, None), (41.10, 28.80, 10.0, 150.0)]
    for lat, lon, radius, elevation in observers:
        expected = base.predict_trajectory(samples, lat, lon, radius, now=now, user_altitude_m=elevation)
        actual = scaled.predict_trajectory(samples, lat, lon, radius, now=now, user_altitude_m=elevation)
        _assert_prediction_equivalent(expected, actual)
    stats = scaled.motion_cache_snapshot()
    assert stats["misses"] == 1
    assert stats["hits"] == 2
    assert stats["entries"] == 1


def test_shared_motion_cache_is_bounded(monkeypatch):
    scaled.reset_motion_cache_for_tests()
    monkeypatch.setattr(scaled, "_MAX_CACHE_ENTRIES", 4)
    now = 1_800_000_000.0
    for index in range(7):
        samples = _samples(now + index * 10.0, lat=41.0 + index * 0.01)
        scaled.shared_motion_projection(samples, max_horizon_s=180, step_s=6)
    stats = scaled.motion_cache_snapshot()
    assert stats["entries"] == 4
    assert stats["max_entries"] == 4


def test_spatial_index_keeps_user_state_isolated():
    users = [_user(1, 41.0, 29.0), _user(2, 41.0, 32.0)]
    aircraft = [_aircraft("abc001", 41.05, 29.05), _aircraft("abc002", 41.05, 32.05)]
    candidates, stats = scale_v53.partition_aircraft_by_user(users, aircraft)
    assert [ac.icao24 for ac in candidates[1]] == ["abc001"]
    assert [ac.icao24 for ac in candidates[2]] == ["abc002"]
    assert stats.full_pairs == 4
    assert stats.candidate_pairs == 2
    assert stats.capped_users == 0


def test_spatial_index_matches_bruteforce_candidate_truth():
    users = [_user(uid, 40.8 + (uid % 10) * 0.05, 28.6 + (uid // 10) * 0.05, radius=20.0) for uid in range(1, 81)]
    aircraft = [_aircraft(f"{index:06x}", 40.6 + (index % 20) * 0.07, 28.3 + (index // 20) * 0.08) for index in range(160)]
    candidates, stats = scale_v53.partition_aircraft_by_user(users, aircraft)
    for user in users:
        uid = user["user_id"]
        loc = user["location"]
        threshold = loc["radius_km"] + scale_v53.EXTRA_TRACKING_MARGIN_KM
        brute = {ac.icao24 for ac in aircraft if scale_v53.haversine(loc["latitude"], loc["longitude"], ac.latitude, ac.longitude) <= threshold}
        indexed = {ac.icao24 for ac in candidates[uid]}
        assert indexed == brute
    assert stats.capped_users == 0
    assert stats.candidate_pairs <= stats.full_pairs
    assert stats.max_candidates_per_user <= len(aircraft)


def test_spatial_index_per_user_work_has_explicit_cap(monkeypatch):
    monkeypatch.setattr(scale_v53, "MAX_CANDIDATE_AIRCRAFT_PER_USER", 5)
    users = [_user(1, 41.0, 29.0, radius=100.0)]
    aircraft = [_aircraft(f"{index:06x}", 41.0, 29.0 + index * 0.001) for index in range(20)]
    candidates, stats = scale_v53.partition_aircraft_by_user(users, aircraft)
    assert len(candidates[1]) == 5
    assert stats.max_candidates_per_user == 5
    assert stats.capped_users == 1
