"""Community self-hosting runtime wrapper for local receiver coverage."""
from __future__ import annotations

from app.local_adsb_coverage_v55 import install_local_receiver_coverage_guard

install_local_receiver_coverage_guard()

from app.main import app  # noqa: E402,F401

__all__ = ["app"]
