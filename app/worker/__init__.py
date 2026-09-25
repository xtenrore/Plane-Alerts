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

    from app.intelligence.trajectory_hotfix_v43 import install_trajectory_hotfix_v43
    install_trajectory_hotfix_v43()

    from app.intelligence.direct_presence_guard_v44 import install_direct_presence_guard_v44
    install_direct_presence_guard_v44()

    from app.intelligence.prediction_v46 import install_prediction_v46
    install_prediction_v46()

    from app.intelligence.route_intelligence_v46 import install_route_intelligence_v46
    install_route_intelligence_v46()

    from app.intelligence.route_observe_guard_v44 import install_route_observe_guard_v44
    install_route_observe_guard_v44()

    # v5.6.1 permanently separates high-volume route/Prediction Lab telemetry
    # from MongoDB.
    from app.operational_volume_v561 import install_operational_volume_v561
    install_operational_volume_v561()

    from app.intelligence.destination_path_guard_v554 import install_destination_path_guard
    install_destination_path_guard()

    from app.worker.critical_timing import install_critical_timing_guards
    install_critical_timing_guards()

    from app.worker.profile_filter_guard_v43 import install_profile_filter_guard_v43
    install_profile_filter_guard_v43()

    from app.worker.cadence_guard_v423 import install_cadence_guard_v423
    install_cadence_guard_v423()

    from app.worker.cadence_due_guard_v424 import install_cadence_due_guard_v424
    install_cadence_due_guard_v424()

    # v4.8 keeps live state in memory and coalesces persistence behind bounded
    # queues. v5.6.2 keeps those queues and replaces their Mongo targets.
    from app.worker.storage_guard_v48 import install_storage_guard_v48
    install_storage_guard_v48()

    # Load photography orchestration before the v5.6.2 installer so retained
    # alert/photo fallback cannot bind the old Mongo accessors because of import
    # order. This import performs no network work or runtime calculation.
    from app.photography import service as _photography_service  # noqa: F401

    from app.operational_state_patch_v562 import install_operational_state_v562
    install_operational_state_v562()

    # app.main imports the Telegram modules before app.worker. Install the
    # interaction layer only in that runtime shape to avoid pulling bot/UI code
    # into standalone worker processes.
    if "app.bot.profile_handlers" in sys.modules:
        from app.bot.interaction_v46 import install_interaction_v46
        install_interaction_v46()
