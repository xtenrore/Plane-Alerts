"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "5.3.0"
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
