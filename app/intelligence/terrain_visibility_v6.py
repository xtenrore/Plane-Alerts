"""Plane Alerts v6.0 deterministic geometric terrain line-of-sight (LOS) engine.

Calculates geometric line of sight from observer elevation to aircraft altitude
using a terrain-elevation profile and Earth-curvature handling.

Rules:
- Return 'blocked' ONLY when verified, adequate-resolution terrain data supports
  that conclusion beyond vertical uncertainty.
- Return 'unknown' for missing, coarse, sparse, or unreliable elevation data, or
  when observer or aircraft altitude is unavailable.
- Keep terrain obstruction strictly distinct from clouds, haze, buildings, and
  other visual conditions. Never promise visual sighting from terrain geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Sequence

from app.intelligence.proximity3d_v51 import (
    AIRCRAFT_ALTITUDE_MAX_M,
    AIRCRAFT_ALTITUDE_MIN_M,
    OBSERVER_ELEVATION_MAX_M,
    OBSERVER_ELEVATION_MIN_M,
)
from app.intelligence.trajectory import haversine_km

TERRAIN_ENGINE_VERSION = "6.0-terrain-los-curvature-v1"
EARTH_RADIUS_M = 6_371_008.8
STANDARD_REFRACTION_K = 4.0 / 3.0
EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M * STANDARD_REFRACTION_K
OBSERVER_EYE_HEIGHT_M = 1.7
MAX_VERIFIED_RESOLUTION_M = 180.0
MAX_PROFILE_SAMPLE_GAP_KM = 2.5
MIN_BLOCK_CONFIDENCE_MARGIN_M = 15.0


@dataclass(slots=True, frozen=True)
class TerrainProfilePoint:
    """Single elevation sample along the great-circle path from observer to aircraft."""

    distance_km: float
    elevation_m: float | None
    latitude: float | None = None
    longitude: float | None = None
    resolution_m: float | None = None


@dataclass(slots=True, frozen=True)
class TerrainProfileData:
    """Terrain profile container with explicit data version and quality metadata."""

    points: tuple[TerrainProfilePoint, ...]
    data_version: str = "unknown"
    quality: str = "missing"  # "verified", "coarse", "unreliable", "missing"
    resolution_m: float | None = None
    source: str = "none"


@dataclass(slots=True, frozen=True)
class TerrainLineOfSightResult:
    """Explainable geometric terrain line-of-sight decision."""

    status: str  # "clear", "blocked", "unknown"
    reason_code: str
    explanation: str
    terrain_data_version: str
    terrain_quality: str
    horizontal_distance_km: float | None
    observer_elevation_m: float | None
    aircraft_altitude_m: float | None
    min_clearance_m: float | None = None
    max_obstruction_m: float | None = None
    blocking_distance_km: float | None = None
    blocking_terrain_elevation_m: float | None = None
    blocking_ray_height_m: float | None = None
    earth_curvature_drop_at_target_m: float | None = None
    elevation_angle_deg: float | None = None
    sample_count: int = 0
    max_gap_km: float | None = None
    visual_conditions_included: bool = False
    scope: str = "geometric_terrain_and_earth_curvature_only"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "terrain_data_version": self.terrain_data_version,
            "terrain_quality": self.terrain_quality,
            "horizontal_distance_km": self.horizontal_distance_km,
            "observer_elevation_m": self.observer_elevation_m,
            "aircraft_altitude_m": self.aircraft_altitude_m,
            "min_clearance_m": self.min_clearance_m,
            "max_obstruction_m": self.max_obstruction_m,
            "blocking_distance_km": self.blocking_distance_km,
            "blocking_terrain_elevation_m": self.blocking_terrain_elevation_m,
            "blocking_ray_height_m": self.blocking_ray_height_m,
            "earth_curvature_drop_at_target_m": self.earth_curvature_drop_at_target_m,
            "elevation_angle_deg": self.elevation_angle_deg,
            "sample_count": self.sample_count,
            "max_gap_km": self.max_gap_km,
            "visual_conditions_included": self.visual_conditions_included,
            "scope": self.scope,
        }


def _finite(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def earth_curvature_drop_m(distance_km: float, *, use_refraction: bool = True) -> float:
    """Return apparent vertical drop in meters due to Earth curvature over distance_km."""
    d_m = max(0.0, float(distance_km)) * 1000.0
    radius_m = EFFECTIVE_EARTH_RADIUS_M if use_refraction else EARTH_RADIUS_M
    return (d_m * d_m) / (2.0 * radius_m)


def geometric_elevation_angle_deg(
    observer_elevation_m: float,
    aircraft_altitude_m: float,
    horizontal_distance_km: float,
    *,
    use_refraction: bool = True,
) -> float:
    """Compute geometric elevation angle from observer eye height to aircraft."""
    d_m = max(1.0, float(horizontal_distance_km) * 1000.0)
    obs_m = float(observer_elevation_m) + OBSERVER_EYE_HEIGHT_M
    drop_m = earth_curvature_drop_m(horizontal_distance_km, use_refraction=use_refraction)
    apparent_delta_m = (float(aircraft_altitude_m) - drop_m) - obs_m
    return math.degrees(math.atan2(apparent_delta_m, d_m))


def evaluate_terrain_line_of_sight(
    *,
    observer_elevation_m: float | None,
    aircraft_altitude_m: float | None,
    horizontal_distance_km: float | None = None,
    observer_lat: float | None = None,
    observer_lon: float | None = None,
    aircraft_lat: float | None = None,
    aircraft_lon: float | None = None,
    profile: TerrainProfileData | Sequence[TerrainProfilePoint | dict[str, Any]] | None = None,
    altitude_uncertainty_m: float = 75.0,
    use_refraction: bool = True,
) -> TerrainLineOfSightResult:
    """Evaluate geometric terrain line of sight between observer and aircraft.

    Never returns 'blocked' unless terrain data is verified, sufficiently dense,
    and the obstruction exceeds the line-of-sight ray by at least the combined
    vertical uncertainty margin.
    """
    obs_elev = _finite(observer_elevation_m)
    ac_alt = _finite(aircraft_altitude_m)
    dist_km = _finite(horizontal_distance_km)
    if dist_km is None and None not in (observer_lat, observer_lon, aircraft_lat, aircraft_lon):
        lat1, lon1 = _finite(observer_lat), _finite(observer_lon)
        lat2, lon2 = _finite(aircraft_lat), _finite(aircraft_lon)
        if None not in (lat1, lon1, lat2, lon2):
            dist_km = haversine_km(float(lat1), float(lon1), float(lat2), float(lon2))

    if isinstance(profile, TerrainProfileData):
        data_version = str(profile.data_version or "unknown")
        quality = str(profile.quality or "missing").lower().strip()
        profile_res_m = _finite(profile.resolution_m)
        raw_points: Iterable[Any] = profile.points
    elif profile is not None:
        data_version = "inline-profile-v1"
        quality = "verified"
        profile_res_m = None
        raw_points = profile
    else:
        data_version = "none"
        quality = "missing"
        profile_res_m = None
        raw_points = ()

    if obs_elev is None or not (OBSERVER_ELEVATION_MIN_M <= obs_elev <= OBSERVER_ELEVATION_MAX_M):
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_OBSERVER_ELEVATION",
            explanation="Observer terrain elevation is missing or outside valid terrestrial bounds.",
            terrain_data_version=data_version,
            terrain_quality=quality if quality != "verified" else "missing_observer_elevation",
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
        )

    if ac_alt is None or not (AIRCRAFT_ALTITUDE_MIN_M <= ac_alt <= AIRCRAFT_ALTITUDE_MAX_M):
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_AIRCRAFT_ALTITUDE",
            explanation="Aircraft altitude is missing or outside physical bounds; terrain LOS cannot be determined.",
            terrain_data_version=data_version,
            terrain_quality=quality,
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
        )

    if dist_km is None or dist_km < 0.0:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_DISTANCE",
            explanation="Horizontal distance between observer and aircraft is unavailable.",
            terrain_data_version=data_version,
            terrain_quality=quality,
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
        )

    target_drop_m = earth_curvature_drop_m(dist_km, use_refraction=use_refraction)
    elev_angle_deg = geometric_elevation_angle_deg(
        obs_elev,
        ac_alt,
        dist_km,
        use_refraction=use_refraction,
    )

    if quality in {"missing", "none", "unavailable"}:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_MISSING_DEM",
            explanation="Terrain elevation profile is unavailable; line of sight is recorded as unknown.",
            terrain_data_version=data_version,
            terrain_quality="missing",
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
        )

    if quality in {"coarse", "unreliable", "degraded"}:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code=f"TERRAIN_UNKNOWN_{quality.upper()}_DATA",
            explanation=f"Terrain data quality is '{quality}'; refusing to claim blocked or clear line of sight.",
            terrain_data_version=data_version,
            terrain_quality=quality,
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
        )

    if profile_res_m is not None and profile_res_m > MAX_VERIFIED_RESOLUTION_M:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_COARSE_RESOLUTION",
            explanation=(
                f"Terrain DEM resolution {profile_res_m:.0f}m exceeds {MAX_VERIFIED_RESOLUTION_M:.0f}m "
                "verified line-of-sight threshold."
            ),
            terrain_data_version=data_version,
            terrain_quality="coarse",
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
        )

    parsed_points: list[TerrainProfilePoint] = []
    for item in raw_points:
        if isinstance(item, TerrainProfilePoint):
            pt = item
        elif isinstance(item, dict):
            pt = TerrainProfilePoint(
                distance_km=_finite(item.get("distance_km")) or 0.0,
                elevation_m=_finite(item.get("elevation_m")),
                latitude=_finite(item.get("latitude")),
                longitude=_finite(item.get("longitude")),
                resolution_m=_finite(item.get("resolution_m")),
            )
        else:
            continue
        if pt.elevation_m is None or not (OBSERVER_ELEVATION_MIN_M <= pt.elevation_m <= OBSERVER_ELEVATION_MAX_M):
            return TerrainLineOfSightResult(
                status="unknown",
                reason_code="TERRAIN_UNKNOWN_INCOMPLETE_PROFILE",
                explanation="Terrain profile contains missing or out-of-bounds sample elevations.",
                terrain_data_version=data_version,
                terrain_quality="unreliable",
                horizontal_distance_km=dist_km,
                observer_elevation_m=obs_elev,
                aircraft_altitude_m=ac_alt,
                earth_curvature_drop_at_target_m=round(target_drop_m, 2),
                elevation_angle_deg=round(elev_angle_deg, 3),
            )
        if pt.resolution_m is not None and pt.resolution_m > MAX_VERIFIED_RESOLUTION_M:
            return TerrainLineOfSightResult(
                status="unknown",
                reason_code="TERRAIN_UNKNOWN_COARSE_SAMPLE",
                explanation=(
                    f"Terrain profile sample resolution {pt.resolution_m:.0f}m is too coarse "
                    f"(>{MAX_VERIFIED_RESOLUTION_M:.0f}m) for authoritative obstruction checks."
                ),
                terrain_data_version=data_version,
                terrain_quality="coarse",
                horizontal_distance_km=dist_km,
                observer_elevation_m=obs_elev,
                aircraft_altitude_m=ac_alt,
                earth_curvature_drop_at_target_m=round(target_drop_m, 2),
                elevation_angle_deg=round(elev_angle_deg, 3),
            )
        parsed_points.append(pt)

    parsed_points.sort(key=lambda p: p.distance_km)
    if dist_km <= 0.35:
        # Immediate overhead/local vicinity (<350 m horizontal)
        if ac_alt + altitude_uncertainty_m >= obs_elev:
            return TerrainLineOfSightResult(
                status="clear",
                reason_code="TERRAIN_CLEAR_IMMEDIATE_VICINITY",
                explanation="Aircraft is within immediate horizontal range above observer elevation.",
                terrain_data_version=data_version,
                terrain_quality=quality,
                horizontal_distance_km=dist_km,
                observer_elevation_m=obs_elev,
                aircraft_altitude_m=ac_alt,
                min_clearance_m=round(ac_alt - obs_elev, 2),
                max_obstruction_m=0.0,
                earth_curvature_drop_at_target_m=round(target_drop_m, 2),
                elevation_angle_deg=round(elev_angle_deg, 3),
                sample_count=len(parsed_points),
                max_gap_km=0.0,
            )

    if len(parsed_points) < 2:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_SPARSE_PROFILE",
            explanation="Fewer than two terrain profile samples are available along the sight line.",
            terrain_data_version=data_version,
            terrain_quality="coarse",
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
            sample_count=len(parsed_points),
        )

    distances = [0.0] + [max(0.0, min(dist_km, p.distance_km)) for p in parsed_points] + [dist_km]
    max_gap_km = max(b - a for a, b in zip(distances, distances[1:]))
    if max_gap_km > MAX_PROFILE_SAMPLE_GAP_KM:
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_PROFILE_GAP",
            explanation=(
                f"Maximum terrain sample spacing {max_gap_km:.2f} km exceeds "
                f"{MAX_PROFILE_SAMPLE_GAP_KM:.2f} km verified profile density threshold."
            ),
            terrain_data_version=data_version,
            terrain_quality="coarse",
            horizontal_distance_km=dist_km,
            observer_elevation_m=obs_elev,
            aircraft_altitude_m=ac_alt,
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
            sample_count=len(parsed_points),
            max_gap_km=round(max_gap_km, 3),
        )

    obs_eye_m = obs_elev + OBSERVER_EYE_HEIGHT_M
    effective_target_m = ac_alt - target_drop_m
    uncertainty_margin_m = max(MIN_BLOCK_CONFIDENCE_MARGIN_M, float(altitude_uncertainty_m or 0.0) * 0.35)

    # Check if aircraft itself is below the Earth curvature horizon relative to sea/observer plane
    min_clearance_m = math.inf
    max_obstruction_m = 0.0
    blocking_dist_km: float | None = None
    blocking_terrain_m: float | None = None
    blocking_ray_m: float | None = None

    for pt in parsed_points:
        d_i = max(0.0, min(dist_km, float(pt.distance_km)))
        if d_i <= 0.05 or abs(dist_km - d_i) <= 0.05:
            continue
        fraction = d_i / max(dist_km, 1e-6)
        # In the tangent-plane frame at the observer, the straight LOS ray has height:
        ray_tangent_m = obs_eye_m + fraction * (effective_target_m - obs_eye_m)
        # Convert ray height back to local MSL at distance d_i by adding the curvature drop at d_i:
        drop_i_m = earth_curvature_drop_m(d_i, use_refraction=use_refraction)
        ray_local_msl_m = ray_tangent_m + drop_i_m
        terrain_msl_m = float(pt.elevation_m)
        clearance_m = ray_local_msl_m - terrain_msl_m
        if clearance_m < min_clearance_m:
            min_clearance_m = clearance_m
            if clearance_m < 0.0:
                max_obstruction_m = -clearance_m
                blocking_dist_km = d_i
                blocking_terrain_m = terrain_msl_m
                blocking_ray_m = ray_local_msl_m

    if min_clearance_m is math.inf:
        min_clearance_m = (effective_target_m - obs_eye_m)

    # Check if blocked beyond uncertainty margin
    if min_clearance_m < -uncertainty_margin_m:
        return TerrainLineOfSightResult(
            status="blocked",
            reason_code="TERRAIN_BLOCKED_RIDGE_OBSTRUCTION",
            explanation=(
                f"Terrain ridge ({blocking_terrain_m:.0f}m MSL at {blocking_dist_km:.2f} km) "
                f"exceeds geometric line-of-sight ray ({blocking_ray_m:.0f}m MSL including Earth curvature) "
                f"by {max_obstruction_m:.1f}m."
            ),
            terrain_data_version=data_version,
            terrain_quality="verified",
            horizontal_distance_km=round(dist_km, 3),
            observer_elevation_m=round(obs_elev, 2),
            aircraft_altitude_m=round(ac_alt, 2),
            min_clearance_m=round(min_clearance_m, 2),
            max_obstruction_m=round(max_obstruction_m, 2),
            blocking_distance_km=round(blocking_dist_km or 0.0, 3),
            blocking_terrain_elevation_m=round(blocking_terrain_m or 0.0, 2),
            blocking_ray_height_m=round(blocking_ray_m or 0.0, 2),
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
            sample_count=len(parsed_points),
            max_gap_km=round(max_gap_km, 3),
        )

    if min_clearance_m < 0.0:
        # Inside the vertical uncertainty band: do not falsely claim blocked or clear
        return TerrainLineOfSightResult(
            status="unknown",
            reason_code="TERRAIN_UNKNOWN_WITHIN_UNCERTAINTY_MARGIN",
            explanation=(
                f"Geometric ray grazes terrain within uncertainty margin "
                f"(obstruction {max_obstruction_m:.1f}m <= {uncertainty_margin_m:.1f}m margin)."
            ),
            terrain_data_version=data_version,
            terrain_quality="verified",
            horizontal_distance_km=round(dist_km, 3),
            observer_elevation_m=round(obs_elev, 2),
            aircraft_altitude_m=round(ac_alt, 2),
            min_clearance_m=round(min_clearance_m, 2),
            max_obstruction_m=round(max_obstruction_m, 2),
            blocking_distance_km=round(blocking_dist_km or 0.0, 3),
            blocking_terrain_elevation_m=round(blocking_terrain_m or 0.0, 2),
            blocking_ray_height_m=round(blocking_ray_m or 0.0, 2),
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
            sample_count=len(parsed_points),
            max_gap_km=round(max_gap_km, 3),
        )

    if effective_target_m < obs_eye_m - uncertainty_margin_m and min_clearance_m <= 0.0:
        return TerrainLineOfSightResult(
            status="blocked",
            reason_code="TERRAIN_BLOCKED_EARTH_CURVATURE_HORIZON",
            explanation=(
                f"Aircraft altitude ({ac_alt:.0f}m) lies below the geometric Earth-curvature "
                f"horizon drop ({target_drop_m:.0f}m) at {dist_km:.1f} km."
            ),
            terrain_data_version=data_version,
            terrain_quality="verified",
            horizontal_distance_km=round(dist_km, 3),
            observer_elevation_m=round(obs_elev, 2),
            aircraft_altitude_m=round(ac_alt, 2),
            min_clearance_m=round(min_clearance_m, 2),
            max_obstruction_m=round(max(0.0, -min_clearance_m), 2),
            earth_curvature_drop_at_target_m=round(target_drop_m, 2),
            elevation_angle_deg=round(elev_angle_deg, 3),
            sample_count=len(parsed_points),
            max_gap_km=round(max_gap_km, 3),
        )

    return TerrainLineOfSightResult(
        status="clear",
        reason_code="TERRAIN_CLEAR_VERIFIED_PROFILE",
        explanation=(
            f"Geometric line of sight is clear across {len(parsed_points)} terrain profile samples "
            f"(minimum vertical clearance {min_clearance_m:.1f}m including Earth curvature; "
            "clouds, haze, buildings and visual conditions are evaluated separately)."
        ),
        terrain_data_version=data_version,
        terrain_quality="verified",
        horizontal_distance_km=round(dist_km, 3),
        observer_elevation_m=round(obs_elev, 2),
        aircraft_altitude_m=round(ac_alt, 2),
        min_clearance_m=round(min_clearance_m, 2),
        max_obstruction_m=0.0,
        earth_curvature_drop_at_target_m=round(target_drop_m, 2),
        elevation_angle_deg=round(elev_angle_deg, 3),
        sample_count=len(parsed_points),
        max_gap_km=round(max_gap_km, 3),
    )
