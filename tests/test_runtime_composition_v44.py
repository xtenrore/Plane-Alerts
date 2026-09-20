import os
import subprocess
import sys
import textwrap


def test_production_guard_stack_installs_in_fresh_interpreter():
    script = textwrap.dedent(
        """
        import app.worker.monitor  # installs production worker guard composition
        from app.intelligence.route_history import RouteHistoryService
        from app.intelligence import route_guard_v2, route_observe_guard_v44, requalification_guard_v43, route_guard_v47
        from app.worker import notifications

        # v4.2 qualification remains wrapped by the v4.3 cancellation latch;
        # v4.7 then adds airport/runway context around that established stack.
        assert RouteHistoryService.evaluate is route_guard_v47.evaluate_route_v47
        assert route_guard_v47.v43.evaluate_route_v43 is requalification_guard_v43.evaluate_route_v43
        assert RouteHistoryService.observe is route_observe_guard_v44.observe_queued
        assert route_guard_v2._historical_paths_cached.__name__ == "_historical_paths_cached"
        assert notifications.send_or_update_approach.__module__ == "app.worker.notifications"
        print("production-guard-stack-ok")
        """
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "production-guard-stack-ok" in result.stdout
