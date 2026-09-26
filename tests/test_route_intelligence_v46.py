import pytest

from app.intelligence.route_history import RouteGateResult, RoutePoint
from app.intelligence.route_intelligence_v46 import (
    RouteHistoryBundle,
    _cluster_paths,
    _decay_weight,
    history_features_v46,
    neutralize_hard_route_veto_v46,
)

USER = (41.0, 29.0)


def path(points):
    return [RoutePoint(lat, lon, float(index)) for index, (lat, lon) in enumerate(points)]


def test_route_history_decay_reduces_old_evidence():
    recent = _decay_weight(1)
    week = _decay_weight(7)
    old = _decay_weight(28)
    assert recent > week > old > 0


def test_route_clustering_groups_similar_paths_and_splits_distinct_path():
    a = path([(41.30, 29.00), (41.20, 29.00), (41.10, 29.00), (41.00, 29.00)])
    b = path([(41.30, 29.01), (41.20, 29.01), (41.10, 29.01), (41.00, 29.01)])
    c = path([(41.30, 29.20), (41.25, 29.30), (41.20, 29.40), (41.15, 29.50)])
    labels = _cluster_paths([a, b, c])
    assert labels[0] == labels[1]
    assert labels[2] != labels[0]


def test_small_route_cluster_explicitly_reduces_confidence():
    current = path([(41.30, 29.00), (41.20, 29.00), (41.15, 29.00)])
    historical = path([(41.32, 29.00), (41.20, 29.00), (41.10, 29.00), (41.00, 29.00)])
    bundle = RouteHistoryBundle([historical], age_days=[1.0], clusters=[0])
    score, label, cpa = history_features_v46(
        current,
        bundle,
        observer_lat=USER[0],
        observer_lon=USER[1],
        radius_km=10.0,
        aircraft_lat=41.15,
        aircraft_lon=29.00,
    )
    assert label == "small-sample"
    assert 0.0 <= score < 0.7
    assert cpa is not None


def test_old_matching_history_has_less_influence_than_recent_history():
    current = path([(41.30, 29.00), (41.20, 29.00), (41.15, 29.00)])
    historical = path([(41.32, 29.00), (41.20, 29.00), (41.10, 29.00), (41.00, 29.00)])
    recent = RouteHistoryBundle([historical, historical, historical], age_days=[1.0, 2.0, 3.0], clusters=[0, 0, 0])
    old = RouteHistoryBundle([historical, historical, historical], age_days=[21.0, 24.0, 28.0], clusters=[0, 0, 0])
    recent_score, _, _ = history_features_v46(
        current,
        recent,
        observer_lat=USER[0],
        observer_lon=USER[1],
        radius_km=10.0,
        aircraft_lat=41.15,
        aircraft_lon=29.00,
    )
    old_score, _, _ = history_features_v46(
        current,
        old,
        observer_lat=USER[0],
        observer_lon=USER[1],
        radius_km=10.0,
        aircraft_lat=41.15,
        aircraft_lon=29.00,
    )
    assert recent_score > old_score


def test_contradictory_route_clusters_are_marked_and_weakened():
    current = path([(41.30, 29.00), (41.20, 29.00), (41.15, 29.00)])
    pass_route = path([(41.32, 29.00), (41.20, 29.00), (41.10, 29.00), (41.00, 29.00)])
    miss_route = path([(41.32, 29.00), (41.20, 29.00), (41.15, 29.00), (41.10, 29.10), (41.05, 29.25)])
    bundle = RouteHistoryBundle(
        [pass_route, pass_route, miss_route, miss_route],
        age_days=[1.0, 2.0, 1.0, 2.0],
        clusters=[0, 0, 1, 1],
    )
    score, label, _ = history_features_v46(
        current,
        bundle,
        observer_lat=USER[0],
        observer_lon=USER[1],
        radius_km=10.0,
        aircraft_lat=41.15,
        aircraft_lon=29.00,
    )
    assert label == "contradictory-clusters"
    assert score < 0.7


def test_destination_is_uncertainty_evidence_not_hard_veto():
    result = RouteGateResult(
        True,
        "THY123",
        "known destination IST is reached before projected observer CPA",
        history_days=3,
        destination_code="IST",
        expected_turn_pending=True,
        route_plausible=True,
    )
    updated = neutralize_hard_route_veto_v46(result)
    assert not updated.suppress_alert
    assert updated.expected_turn_pending
    assert "live geometry remains authoritative" in updated.reason


def test_diverged_history_cannot_hard_suppress_live_geometry():
    result = RouteGateResult(
        True,
        "THY123",
        "today's route diverges from the recent flight-number pattern",
        history_days=3,
        route_plausible=True,
    )
    updated = neutralize_hard_route_veto_v46(result)
    assert not updated.suppress_alert
    assert not updated.expected_turn_pending
