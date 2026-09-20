"""Deterministic Plane Alerts spotting-intelligence engines."""

# Production reliability guard: loaded once with the intelligence package so
# every route-gate evaluation gets the destination-near-observer protection.
from app.intelligence import arrival_guard_hotfix as _arrival_guard_hotfix  # noqa: F401,E402

# v4.7.1 global airport/runway reference layer. This is local SQLite data built
# into the production image; failure degrades safely to existing metadata.
from app.intelligence import global_airports_v471 as _global_airports_v471  # noqa: F401,E402
