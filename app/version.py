"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os
from pathlib import Path

VERSION = "4.4.0"
PREDICTION_VERSION = "4.4-observation-confirmations"


def _runtime_commit() -> str:
    """Return deployment-provided commit metadata for the exact running build."""
    for name in ("RAILWAY_GIT_COMMIT_SHA", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = os.getenv(name, "").strip()
        if value:
            return value

    # GitHub's Railway workflow generates this uncommitted file immediately
    # before ``railway up`` from the SHA that just passed main CI. Keeping the
    # file out of git prevents an old release SHA from ever becoming source
    # truth while still making CLI deployments self-identifying at runtime.
    build_file = Path(__file__).with_name("build_commit.txt")
    if build_file.exists():
        value = build_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    return "unknown"


COMMIT = _runtime_commit()
