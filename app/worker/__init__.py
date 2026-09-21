"""Background worker sub-package."""

import sys

# Keep unit-test imports side-effect free. Production imports of app.worker.*
# install the monitoring reliability guards before monitor.py binds its symbols.
if "pytest" not in sys.modules:
    from app.worker.reliability import install_reliability_guards

    install_reliability_guards()

    # v5.3 changes only the observer-independent base motion projection. Install
    # it before every established trajectory safety wrapper is imported so the
    # wrapper chain remains: shared base -> v4.3 -> v4.4 -> v4.6 -> critical
    # timing. Never replace monitor.predict_trajectory after that chain binds.
    from app.intelligence import trajectory as _trajectory_core
    from app.intelligence.trajectory_scale_v53 import predict_trajectory as _predict_trajectory_v53

    _trajectory_core.predict_trajectory = _predict_trajectory_v53

    # Keep the robust trajectory predictor but remove systematic full-step
    # acceleration/turn integration bias.
    from app.intelligence.trajectory_hotfix_v43 import install_trajectory_hotfix_v43

    install_trajectory_hotfix_v43()

    # Fresh direct physical presence inside the configured radius can recover an
    # otherwise-uncertain close pass without weakening ordinary thresholds.
    from app.intelligence.direct_presence_guard_v44 import install_direct_presence_guard_v44

    install_direct_presence_guard_v44()

    # v4.6 adds speed-dependent freshness, uncertainty and formal confidence
    # evidence around the established CPA geometry.
    from app.intelligence.prediction_v46 import install_prediction_v46

    install_prediction_v46()

    # Route resolver/read caches are installed before the bounded writer so the
    # route persistence target retains the newer 35-day v4.6 behavior.
    from app.intelligence.route_guard import install_route_guard
    from app.intelligence.route_guard_v2 import install_route_guard_v2

    install_route_guard()
    install_route_guard_v2()

    from app.intelligence.route_intelligence_v46 import install_route_intelligence_v46

    install_route_intelligence_v46()

    from app.intelligence.route_observe_guard_v44 import install_route_observe_guard_v44
    from app.intelligence.route_guard_v42 import install_route_guard_v42
    from app.intelligence.requalification_guard_v43 import install_requalification_guard_v43

    install_route_observe_guard_v44()
    install_route_guard_v42()
    install_requalification_guard_v43()

    # Maintain current LTFM runway data before terminal/runway inference.
    from app.intelligence.runway_data_v47 import install_current_runway_data_v47

    install_current_runway_data_v47()

    # v4.7 terminal/runway evidence and the narrow v4.7.2 initial hold remain
    # authoritative exactly as before this storage-only release.
    from app.intelligence.route_guard_v47 import install_route_guard_v47

    install_route_guard_v47()

    # Cap individual provider latency and prevent stale ADS-B positions from
    # creating brand-new approach alerts.
    from app.worker.critical_timing import install_critical_timing_guards

    install_critical_timing_guards()

    # v4.3 profile filtering runs before v4.2.3 batching so one state snapshot
    # covers all custom-radius groups in a user evaluation.
    from app.worker.profile_filter_guard_v43 import install_profile_filter_guard_v43

    install_profile_filter_guard_v43()

    # v4.2.3 bounds provider latency, batches state reads and moves provider
    # learning out of the five-second path.
    from app.worker.cadence_guard_v423 import install_cadence_guard_v423

    install_cadence_guard_v423()

    # v4.2.4 prevents ordinary scheduler jitter from creating ~10-second skips.
    from app.worker.cadence_due_guard_v424 import install_cadence_due_guard_v424

    install_cadence_due_guard_v424()

    # v4.8 installs last so it can replace only the remaining storage-facing
    # hooks from v4.2.3/v4.4. Prediction, route and notification semantics are
    # deliberately left untouched.
    from app.worker.storage_guard_v48 import install_storage_guard_v48

    install_storage_guard_v48()

    # Notification telemetry is integrated directly in notifications.py and
    # runs through its own bounded post-delivery queue. Do not wrap it again.

    # app.main imports the Telegram modules before app.worker. Install the
    # interaction layer only in that runtime shape to avoid pulling bot/UI code
    # into standalone worker processes.
    if "app.bot.profile_handlers" in sys.modules:
        from app.bot.interaction_v46 import install_interaction_v46

        install_interaction_v46()
