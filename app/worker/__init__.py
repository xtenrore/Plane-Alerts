"""Background worker sub-package."""

import sys

# Keep unit-test imports side-effect free. Production imports of app.worker.*
# install the monitoring reliability guards before monitor.py binds its symbols.
if "pytest" not in sys.modules:
    from app.worker.reliability import install_reliability_guards

    install_reliability_guards()

    # Install the midpoint trajectory integrator before monitor.py imports
    # predict_trajectory by name. This keeps the robust v3.4/v4.2 predictor but
    # removes the systematic full-step acceleration/turn integration bias.
    from app.intelligence.trajectory_hotfix_v43 import install_trajectory_hotfix_v43

    install_trajectory_hotfix_v43()

    # The v4.4 direct-presence guard runs after the midpoint predictor. It does
    # not weaken normal confidence thresholds: only two fresh independent ADS-B
    # positions physically inside the configured radius can promote an
    # otherwise-uncertain close pass into the existing qualification pipeline.
    from app.intelligence.direct_presence_guard_v44 import install_direct_presence_guard_v44

    install_direct_presence_guard_v44()

    # v4.6 keeps the established CPA geometry authoritative while adding
    # speed-dependent freshness, explicit uncertainty, formal confidence
    # evidence, and linear/turn-aware shadow candidates.
    from app.intelligence.prediction_v46 import install_prediction_v46

    install_prediction_v46()

    # Install the destination adapters, the v2 non-blocking route resolver/read
    # cache, then the bounded route-history write queue. The v4.2 ensemble
    # qualification guard and v4.3 cancellation latch are layered afterward.
    from app.intelligence.route_guard import install_route_guard
    from app.intelligence.route_guard_v2 import install_route_guard_v2
    from app.intelligence.route_observe_guard_v44 import install_route_observe_guard_v44
    from app.intelligence.route_guard_v42 import install_route_guard_v42
    from app.intelligence.requalification_guard_v43 import install_requalification_guard_v43

    install_route_guard()
    install_route_guard_v2()
    install_route_observe_guard_v44()
    install_route_guard_v42()
    install_requalification_guard_v43()

    # Route clustering/decay is built only by the v2 bounded background refresh.
    # It weakens stale/contradictory history instead of turning it into a veto.
    from app.intelligence.route_intelligence_v46 import install_route_intelligence_v46

    install_route_intelligence_v46()

    # Final hot-path protection: cap individual provider latency and prevent
    # stale ADS-B positions from creating brand-new approach alerts.
    from app.worker.critical_timing import install_critical_timing_guards

    install_critical_timing_guards()

    # v4.3 applies active-profile/category/aircraft filtering before the legacy
    # matcher. Install it before v4.2.3 batching so one state prefetch still
    # covers the whole user evaluation even when custom radii create groups.
    from app.worker.profile_filter_guard_v43 import install_profile_filter_guard_v43

    install_profile_filter_guard_v43()

    # v4.2.3 removes database fan-out and non-critical learning work from the
    # five-second alert path, and bounds a single shared provider refresh.
    from app.worker.cadence_guard_v423 import install_cadence_guard_v423

    install_cadence_guard_v423()

    # v4.2.4 prevents normal scheduler jitter from turning a near-five-second
    # evaluation interval into a skipped cycle and roughly ten-second gap.
    from app.worker.cadence_due_guard_v424 import install_cadence_due_guard_v424

    install_cadence_due_guard_v424()

    # Notification telemetry is integrated directly in notifications.py and
    # runs through its own bounded post-delivery queue. Do not install a second
    # wrapper around the sender: edits, retries and lifecycle updates must be
    # recorded once against the same logical alert.
