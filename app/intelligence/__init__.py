"""Deterministic Plane Alerts spotting-intelligence engines."""

# v4.7.1 global airport data remains available to diagnostics and optional
# airport/runway analysis. It is not a live alert authority.
from app.intelligence import global_airports_v471 as _global_airports_v471  # noqa: F401,E402
