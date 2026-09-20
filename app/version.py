"""Canonical release identifiers for Plane Alerts runtime and reporting."""
from __future__ import annotations

import os

VERSION = "4.4.0"
PREDICTION_VERSION = "4.4-observation-confirmations"


def _runtime_commit() -> str:
    """Return the commit injected by the deployment/runtime environment."""
    for name in ("RAILWAY_GIT_COMMIT_SHA", "GITHUB_SHA", "SOURCE_COMMIT"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return "unknown"


COMMIT = _runtime_commit()
