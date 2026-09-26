"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "5.7.1"
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
# v5.5.1 moves Prediction Lab audit/shadow evidence from high-volume Mongo writes
# to a bounded persistent file spool and Git data branch.
# v5.5.2 fixes Railway deployment/volume synchronization.
# v5.5.3 isolates historical maintenance from live readiness.
# v5.5.4 installs provider-first destination/path qualification.
# v5.6 removes all remaining retired external-agent residue.
# v5.6.1 moves callsign route history and Prediction Lab compatibility telemetry
# to the Plane Alerts persistent volume while retaining Mongo for durable app data.
# v5.6.2 retires legacy Prediction Lab Mongo exporters/collections.
# v5.6.3 moves notification lifecycle telemetry to bounded volume SQLite,
# retires the Mongo notification/route shells, and enforces a runtime policy that
# prevents high-volume operational Mongo indexes/accessors from reappearing.
# v5.6.4 makes the v5.6.3 operational-Mongo retirement strictly idempotent so
# completed cleanup never reopens Mongo, re-drops retired collections or emits
# repeated retirement logs during normal notification processing.
# v5.6.5 restores and maintains persistent-volume headroom by pruning only
# repository-acknowledged *.synced Prediction Lab evidence; unsynced evidence,
# route/notification SQLite data and durable application data remain untouched.
# v5.6.6 recovers a critically full Plane Alerts volume by removing only an
# incomplete, locally unverified historical operational-Mongo export. A verified
# archive, raw unsynced evidence, runtime SQLite stores and user/application data
# are explicitly protected. It also logs a read-only volume size inventory.
# v5.6.7 fixes Prediction Lab raw-spool discovery/drain, adds lossless NDJSON
# compaction, and reserves disk headroom by shedding optional analytical evidence
# before it can starve operational storage or create an ENOSPC traceback storm.
# v5.6.8 removes only repository-acknowledged spool objects immediately after a
# successful evidence-branch push and chains bounded workflow-dispatch drain passes
# while a saturated backlog remains. A main-workflow push trigger bootstraps the
# first drain pass instead of relying solely on delayed GitHub schedule delivery.
# v5.6.9 targets the live service filesystem for Prediction Lab synchronization,
# because production proved the selected-volume SFTP target could resolve the
# volume attachment yet fail listing /raw. Production then proved Railway file
# transport itself requires an SSH key unavailable to project-token CI.
# v5.6.10 replaces Railway SFTP with a bounded admin-authenticated HTTP bridge.
# GitHub independently validates each raw evidence byte/hash/schema, pushes it to
# prediction-lab-data, then acknowledges exact path+size+SHA before runtime unlink.
# v5.6.11 keeps malformed, legacy, sensitive or oversized raw objects untouched
# while continuing to export valid evidence behind them, preventing one isolated
# object from blocking the repository-acknowledged recovery of the full volume.
# v5.7.1 is the cumulative Bug Fixes Update that consolidates the verified v5.6.x
# storage/reliability recovery as the next public release identity. It adds no new
# trajectory, CPA, ETA, qualification, cancellation, destination-path or timing logic.
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
