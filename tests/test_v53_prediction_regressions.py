from __future__ import annotations

import pytest

from app.intelligence import trajectory as trajectory
from app.intelligence.trajectory_hotfix_v43 import predict_trajectory_v43
from app.version import PREDICTION_VERSION, VERSION


def _samples(*, position_age_s: float):
    now = 2_000_000_000.0
    return [
        trajectory.HistorySample(now - 10.0, 41.20, 29.0, 3000.0, 300.0, 180.0, 0.0, 0.0),
        trajectory.HistorySample(now, 41.16, 29.0, 3000.0, 300.0, 180.0, 0.0, position_age_s),
    ]


def test_v53_release_identity():
    assert tuple(int(part) for part in VERSION.split(".")) >= (5, 3, 0)
    assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"


def test_midpoint_eta_preserves_provider_position_age():
    now = 2_000_000_000.0
    fresh = predict_trajectory_v43(_samples(position_age_s=0.0), 41.0, 29.0, 15.0, now=now)
    delayed = predict_trajectory_v43(_samples(position_age_s=4.0), 41.0, 29.0, 15.0, now=now)
    assert fresh.time_to_cpa_s is not None
    assert delayed.time_to_cpa_s is not None
    assert delayed.time_to_cpa_s == pytest.approx(max(0.0, fresh.time_to_cpa_s - 4.0), abs=0.05)
    assert delayed.time_to_cpa_s <= fresh.time_to_cpa_s
