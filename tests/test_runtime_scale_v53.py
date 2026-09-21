from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_v53_scale_layer_is_installed_through_full_production_predictor_chain():
    """Exercise the same import composition used by the Railway worker.

    Unit tests normally import under pytest, where app.worker intentionally skips
    production monkey-patch installation. A fresh interpreter catches signature
    or ordering regressions that direct module tests cannot see.
    """
    script = textwrap.dedent(
        """
        import app.worker.monitor as monitor
        from app.intelligence import midpoint_scale_v53, trajectory, trajectory_hotfix_v43, trajectory_scale_v53
        from app.version import PREDICTION_VERSION, VERSION
        from app.worker import critical_timing

        assert tuple(int(part) for part in VERSION.split('.')) >= (5, 3, 0)
        assert PREDICTION_VERSION == "5.3-3d-proximity-age-aware"
        assert trajectory_hotfix_v43._ORIGINAL_PREDICT is trajectory_scale_v53.predict_trajectory
        assert trajectory.predict_trajectory is critical_timing._critical_predict_trajectory
        assert monitor.predict_trajectory is critical_timing._critical_predict_trajectory

        trajectory_scale_v53.reset_motion_cache_for_tests()
        midpoint_scale_v53.reset_midpoint_cache_for_tests()
        now = 2_000_000_000.0
        samples = [
            trajectory.HistorySample(now - 10.0, 41.20, 29.0, 3000.0, 300.0, 180.0, 0.0, 4.0),
            trajectory.HistorySample(now, 41.16, 29.0, 3000.0, 300.0, 180.0, 0.0, 4.0),
        ]
        first = monitor.predict_trajectory(samples, 41.0, 29.0, 15.0, now=now, user_altitude_m=None, altitude_relevance=True)
        second = monitor.predict_trajectory(samples, 41.02, 29.01, 15.0, now=now, user_altitude_m=80.0, altitude_relevance=False)
        assert first.time_to_cpa_s is not None
        assert second.time_to_cpa_s is not None
        assert first.stale is False
        assert second.stale is False
        motion = trajectory_scale_v53.motion_cache_snapshot()
        midpoint = midpoint_scale_v53.midpoint_cache_snapshot()
        assert motion["entries"] == 1 and motion["hits"] >= 1
        assert midpoint["entries"] == 1 and midpoint["hits"] >= 1
        print("v53-production-scale-chain-ok")
        """
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", script], cwd=os.getcwd(), env=env, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert "v53-production-scale-chain-ok" in result.stdout
