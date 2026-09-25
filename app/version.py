"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "5.6.2"
# v5.1 adds deterministic altitude-aware relevance and true 3D CPA while
# retaining horizontal CPA as the mandatory safe fallback for uncertain data.
# v5.1.1 changes release/deployment infrastructure only.
# v5.1.2 restores compatibility across the installed v4.6/v5.1 predictor
# wrapper chain without changing the physical model itself.
# v5.1.3 completes the same compatibility contract through the older v4.4 and
# v4.3 wrappers discovered by live production verification.
# v5.2 adds shadow-only automatic evaluation and release-to-release metrics.
# v5.2.1 corrects release documentation/identity only; the physical model and
# v5.2 evaluator behavior are unchanged.
# v5.3 reuses observer-independent aircraft motion across users, adds bounded
# spatial candidate filtering, and preserves provider-reported position age in
# the authoritative midpoint ETA correction.
# v5.4 is a user-experience release: presets, profile navigation/recovery,
# validation and Mini App terminology change without modifying physical logic.
# v5.4.2 fixes general-audit setup, storage, self-hosting, interaction,
# enrichment and photography correctness issues.
# v5.4.3 removes the retired external agent sidecar/runtime and Telegram command.
# v5.5.1 carries the file-backed Prediction Lab architecture forward because
# the immutable v5.5.0 tag was already published for guided community self-hosting.
# It moves Prediction Lab audit/shadow evidence from high-volume Mongo writes
# to a bounded persistent file spool and Git data branch. It does not change
# live physical prediction, qualification, cancellation or alert timing.
# v5.5.2 fixes Railway CLI selector ordering in production deployment and
# Prediction Lab volume synchronization after v5.5.1 exposed the CLI mismatch.
# v5.5.3 isolates verified Prediction Lab archive/index/schema maintenance from
# Railway readiness so historical migration retries in the background while
# normal Mongo-backed monitoring can start immediately.
# v5.5.4 replaces stacked route-history/runway arrival vetoes with one cached,
# provider-first destination/path qualification guard. The underlying motion/3D
# predictor is unchanged; only destination-aware alert qualification changes.
# v5.6 removes all remaining retired external-agent names/hand-off residue from
# active source, tests and documentation. Prediction behavior is unchanged.
# v5.6.1 permanently moves callsign route-history and other high-volume
# Prediction Lab compatibility telemetry off MongoDB and onto the Plane Alerts
# persistent volume. Mongo remains authoritative for users, locations, profiles
# and durable configuration. Live prediction/qualification geometry is unchanged.
# v5.6.2 extends that boundary to alert lifecycle restart state, notification
# telemetry, photo snapshots, worker status and generic analytical persistence.
# These short-lived/high-churn records are volume-only; durable user/config data
# remains in MongoDB. Prediction/qualification/cancellation behavior is unchanged.
PREDICTION_VERSION = "5.3-3d-proximity-age-aware"


def _runtime_commit() -> str:
    """Return deployment-provided commit metadata for the exact running build."""
    for name in ("RAILWAY_GIT_COMMIT_SHA", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    build_file = Path(__file__).with_name("build_commit.txt")
    if build_file.exists():
        value = build_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "unknown"


COMMIT = _runtime_commit()
