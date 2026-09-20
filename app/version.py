"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "5.1.1"
# v5.1 adds deterministic altitude-aware relevance and true 3D CPA while
# retaining horizontal CPA as the mandatory safe fallback for uncertain data.
# v5.1.1 changes release/deployment infrastructure only.
PREDICTION_VERSION = "5.1-3d-proximity"


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
