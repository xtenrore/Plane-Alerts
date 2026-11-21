/**
 * Plane Alerts v6.0.0 — Pure Deterministic Diagnostic, Airport/Arrival, Terrain LOS & ETA/CPA Engine.
 *
 * Mirrors the Python v6 modules (`diagnostics_v6.py`, `airport_arrival_v6.py`,
 * `terrain_visibility_v6.py`, `eta_cpa_v6.py`) for unit testing and interactive simulation.
 */

export const VERSION = "6.0.0";
export const PREDICTION_VERSION = "5.3-3d-proximity-age-aware";
export const DEFAULT_PINNED_MAIN_SHA = "e856aa3449d7f509af0b2f853b5c814ab28d0eb5";
export const ROLLBACK_TARGET_RELEASE = "v5.8.8";
export const COMMUNITY_ROLLBACK_RELEASE = "v5.5.3";

export const EARTH_RADIUS_M = 6_371_008.8;
export const STANDARD_REFRACTION_K = 4.0 / 3.0;
export const EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M * STANDARD_REFRACTION_K;
export const OBSERVER_EYE_HEIGHT_M = 1.7;
export const MAX_VERIFIED_RESOLUTION_M = 180.0;
export const MAX_PROFILE_SAMPLE_GAP_KM = 2.5;
export const MIN_BLOCK_CONFIDENCE_MARGIN_M = 15.0;

export type UserFeedbackCategoryV6 =
  | "saw_it"
  | "terrain_blocked"
  | "poor_visibility"
  | "wrong_timing_path"
  | "unsure";

export const USER_FEEDBACK_CATEGORIES_V6: readonly UserFeedbackCategoryV6[] = [
  "saw_it",
  "terrain_blocked",
  "poor_visibility",
  "wrong_timing_path",
  "unsure",
];

export type TriageProblemClassV6 =
  | "DATA_FRESHNESS"
  | "ETA_TRAJECTORY"
  | "AIRPORT_LOGIC"
  | "TERRAIN_VISIBILITY"
  | "NOTIFICATION_LIFECYCLE";

export type AirportClassificationV6 =
  | "ARRIVAL"
  | "DEPARTURE"
  | "HOLDING"
  | "MISSED_APPROACH"
  | "OVERFLIGHT"
  | "AIRPORT_NEARBY"
  | "NONE";

export type TerrainLosStatusV6 = "clear" | "blocked" | "unknown";

export type EtaRootCauseDriverV6 =
  | "OLD_OBSERVATIONS"
  | "CHANGING_TRAJECTORY"
  | "AIRPORT_PATH_HANDLING"
  | "DELIVERY_TIMING"
  | "STABLE_NOMINAL";

export interface RunwayThresholdInput {
  runwayId: string;
  thresholdLat: number;
  thresholdLon: number;
  headingDeg: number;
  oppositeLat: number;
  oppositeLon: number;
}

export interface AirportGeometryInput {
  icao: string;
  iata: string;
  name: string;
  latitude: number;
  longitude: number;
  elevationM: number;
  runways: RunwayThresholdInput[];
}

export interface AircraftEvaluationInput {
  icao24: string;
  callsign: string;
  latitude: number;
  longitude: number;
  altitudeM: number | null;
  groundSpeedKts: number;
  headingDeg: number;
  verticalRateMps: number;
  positionAgeS: number | null;
  source: string;
  destinationCode?: string;
  holdingScore?: number;
  previousLowApproach?: boolean;
  turnRateDegS?: number;
  deliveryLagS?: number;
}

export interface TerrainSamplePoint {
  distanceKm: number;
  elevationM: number | null;
  resolutionM?: number;
}

export interface TerrainProfileInput {
  points: TerrainSamplePoint[];
  dataVersion: string;
  quality: "verified" | "coarse" | "unreliable" | "missing";
  resolutionM?: number;
}

export interface TerrainLosEvaluation {
  status: TerrainLosStatusV6;
  reasonCode: string;
  explanation: string;
  terrainDataVersion: string;
  terrainQuality: string;
  horizontalDistanceKm: number;
  observerElevationM: number | null;
  aircraftAltitudeM: number | null;
  minClearanceM: number | null;
  maxObstructionM: number;
  blockingDistanceKm: number | null;
  blockingTerrainElevationM: number | null;
  earthCurvatureDropAtTargetM: number;
  elevationAngleDeg: number;
  visualConditionsIncluded: false;
  scope: "geometric_terrain_and_earth_curvature_only";
}

export interface AirportArrivalEvaluation {
  classification: AirportClassificationV6;
  matchedAirportIcao: string;
  matchedAirportIata: string;
  matchedAirportName: string;
  matchedGeometry:
    | "runway_threshold"
    | "extended_centerline"
    | "airport_boundary"
    | "terminal_corridor"
    | "none";
  matchedRunwayId: string;
  airportDistanceKm: number;
  thresholdDistanceKm: number | null;
  crossTrackKm: number | null;
  alongTrackBeforeThresholdKm: number | null;
  headingDeltaDeg: number | null;
  aglM: number | null;
  observerBeforeAirport: boolean;
  suppressAlert: boolean;
  reasonCode: string;
  explanation: string;
  trackEvidence: string[];
}

export interface EtaCpaEvaluation {
  primaryDriver: EtaRootCauseDriverV6;
  reasonCode: string;
  explanation: string;
  effectiveAgeS: number;
  freshnessLimitS: number;
  ageCorrectedTimeToCpaS: number | null;
  ageCorrectedRadiusEntryS: number | null;
  etaUncertaintyS: number | null;
  cpaUncertaintyKm: number;
  etaAvailable: boolean;
  etaReliable: boolean;
}

export interface DecisionDiagnosticRecordV6 {
  recordId: string;
  mainSha: string;
  icao: string;
  callsign: string;
  observerRef: string;
  observationSource: string;
  observationAgeS: number;
  freshnessStatus: "fresh" | "aging" | "stale" | "missing_unknown";
  coverageStatus: "confirmed" | "unknown";
  relativeDistanceKm: number;
  relativeBearingDeg: number;
  altitudeM: number | null;
  groundSpeedKts: number;
  headingDeg: number;
  predictedCpaKm: number;
  predictedCpa3dKm: number;
  predictedEtaS: number | null;
  confidenceLabel: "High" | "Medium" | "Low" | "Uncertain";
  airportEvaluation: AirportArrivalEvaluation;
  terrainEvaluation: TerrainLosEvaluation;
  etaCpaEvaluation: EtaCpaEvaluation;
  finalDecision: "alert" | "suppress" | "cancel" | "hold" | "unknown_coverage" | "delivery_failure";
  stableReasonCode: string;
  userFeedback: UserFeedbackCategoryV6 | null;
  userFeedbackNote: string | null;
}

export function haversineKm(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180.0;
  const p1 = toRad(lat1);
  const p2 = toRad(lat2);
  const dlat = p2 - p1;
  const dlon = toRad(lon2 - lon1);
  const a =
    Math.sin(dlat / 2) ** 2 +
    Math.cos(p1) * Math.cos(p2) * Math.sin(dlon / 2) ** 2;
  return 6371.0088 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(Math.max(0.0, 1 - a)));
}

export function bearingDeg(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const toRad = (deg: number) => (deg * Math.PI) / 180.0;
  const toDeg = (rad: number) => (rad * 180.0) / Math.PI;
  const p1 = toRad(lat1);
  const p2 = toRad(lat2);
  const dlon = toRad(lon2 - lon1);
  const y = Math.sin(dlon) * Math.cos(p2);
  const x =
    Math.cos(p1) * Math.sin(p2) -
    Math.sin(p1) * Math.cos(p2) * Math.cos(dlon);
  return (toDeg(Math.atan2(y, x)) + 360.0) % 360.0;
}

export function angleDeltaDeg(a: number, b: number): number {
  return ((a - b + 540.0) % 360.0) - 180.0;
}

export function anonymizeObserverRef(
  lat: number | null,
  lon: number | null,
  observerId = "obs",
): string {
  const latFixed = (lat ?? 0).toFixed(3);
  const lonFixed = (lon ?? 0).toFixed(3);
  const raw = `${observerId}:${latFixed}:${lonFixed}`;
  let h1 = 0xdeadbeef ^ raw.length;
  let h2 = 0x41c6ce57 ^ raw.length;
  for (let i = 0; i < raw.length; i++) {
    const ch = raw.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 2654435761);
    h2 = Math.imul(h2 ^ ch, 1597334677);
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  const hex = (4294967296 * (2097151 & h2) + (h1 >>> 0)).toString(16).padStart(12, "0");
  return `obs_${hex.slice(0, 12)}`;
}

export function redactSensitiveConfig(
  input: Record<string, unknown>,
): Record<string, unknown> {
  const sensitiveTokens = [
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "private_key",
    "credential",
    "webhook",
    "mongo_uri",
    "database_url",
    "lat",
    "lon",
    "latitude",
    "longitude",
  ];
  const out: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(input)) {
    const lower = key.toLowerCase();
    if (sensitiveTokens.some((tok) => lower.includes(tok))) {
      out[key] = val === "" || val === null || val === undefined ? "" : "<REDACTED>";
    } else if (val && typeof val === "object" && !Array.isArray(val)) {
      out[key] = redactSensitiveConfig(val as Record<string, unknown>);
    } else {
      out[key] = val;
    }
  }
  return out;
}

export function classifyObservationFreshness(
  observationAgeS: number | null,
  hasPosition = true,
  staleThresholdS = 65.0,
  agingThresholdS = 25.0,
): {
  freshnessStatus: "fresh" | "aging" | "stale" | "missing_unknown";
  coverageStatus: "confirmed" | "unknown";
} {
  if (!hasPosition || observationAgeS === null || !Number.isFinite(observationAgeS)) {
    return { freshnessStatus: "missing_unknown", coverageStatus: "unknown" };
  }
  const age = Math.max(0, observationAgeS);
  if (age > staleThresholdS) {
    return { freshnessStatus: "stale", coverageStatus: "unknown" };
  }
  if (age > agingThresholdS) {
    return { freshnessStatus: "aging", coverageStatus: "confirmed" };
  }
  return { freshnessStatus: "fresh", coverageStatus: "confirmed" };
}

export function earthCurvatureDropM(distanceKm: number, useRefraction = true): number {
  const dM = Math.max(0.0, distanceKm) * 1000.0;
  const radiusM = useRefraction ? EFFECTIVE_EARTH_RADIUS_M : EARTH_RADIUS_M;
  return (dM * dM) / (2.0 * radiusM);
}

export function evaluateTerrainLineOfSightV6(params: {
  observerElevationM: number | null;
  aircraftAltitudeM: number | null;
  horizontalDistanceKm: number;
  profile: TerrainProfileInput | null;
  altitudeUncertaintyM?: number;
  useRefraction?: boolean;
}): TerrainLosEvaluation {
  const {
    observerElevationM,
    aircraftAltitudeM,
    horizontalDistanceKm,
    profile,
    altitudeUncertaintyM = 75.0,
    useRefraction = true,
  } = params;

  const dataVersion = profile?.dataVersion ?? "none";
  const quality = profile?.quality ?? "missing";
  const targetDropM = Number(earthCurvatureDropM(horizontalDistanceKm, useRefraction).toFixed(2));

  if (observerElevationM === null || !Number.isFinite(observerElevationM) || observerElevationM < -450 || observerElevationM > 9000) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_OBSERVER_ELEVATION",
      explanation: "Observer terrain elevation is missing or outside valid terrestrial bounds.",
      terrainDataVersion: dataVersion,
      terrainQuality: "missing_observer_elevation",
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg: 0,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  if (aircraftAltitudeM === null || !Number.isFinite(aircraftAltitudeM) || aircraftAltitudeM < -450 || aircraftAltitudeM > 60000) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_AIRCRAFT_ALTITUDE",
      explanation: "Aircraft altitude is missing or outside physical bounds; terrain LOS cannot be determined.",
      terrainDataVersion: dataVersion,
      terrainQuality: quality,
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg: 0,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  const dM = Math.max(1.0, horizontalDistanceKm * 1000.0);
  const obsEyeM = observerElevationM + OBSERVER_EYE_HEIGHT_M;
  const effectiveTargetM = aircraftAltitudeM - targetDropM;
  const elevationAngleDeg = Number(
    ((Math.atan2(effectiveTargetM - obsEyeM, dM) * 180.0) / Math.PI).toFixed(3),
  );

  if (!profile || quality === "missing" || quality === "coarse" || quality === "unreliable") {
    return {
      status: "unknown",
      reasonCode: quality === "missing" ? "TERRAIN_UNKNOWN_MISSING_DEM" : `TERRAIN_UNKNOWN_${quality.toUpperCase()}_DATA`,
      explanation: `Terrain elevation profile is '${quality}'; refusing to claim blocked or clear line of sight.`,
      terrainDataVersion: dataVersion,
      terrainQuality: quality,
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  if (profile.resolutionM !== undefined && profile.resolutionM > MAX_VERIFIED_RESOLUTION_M) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_COARSE_RESOLUTION",
      explanation: `Terrain DEM resolution ${profile.resolutionM}m exceeds ${MAX_VERIFIED_RESOLUTION_M}m verified line-of-sight threshold.`,
      terrainDataVersion: dataVersion,
      terrainQuality: "coarse",
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  const points = [...profile.points].sort((a, b) => a.distanceKm - b.distanceKm);
  if (points.length < 2) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_SPARSE_PROFILE",
      explanation: "Fewer than two terrain profile samples are available along the sight line.",
      terrainDataVersion: dataVersion,
      terrainQuality: "coarse",
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  const dists = [0, ...points.map((p) => Math.max(0, Math.min(horizontalDistanceKm, p.distanceKm))), horizontalDistanceKm];
  let maxGapKm = 0;
  for (let i = 0; i < dists.length - 1; i++) {
    maxGapKm = Math.max(maxGapKm, dists[i + 1] - dists[i]);
  }
  if (maxGapKm > MAX_PROFILE_SAMPLE_GAP_KM) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_PROFILE_GAP",
      explanation: `Maximum terrain sample spacing ${maxGapKm.toFixed(2)} km exceeds ${MAX_PROFILE_SAMPLE_GAP_KM.toFixed(2)} km verified profile density threshold.`,
      terrainDataVersion: dataVersion,
      terrainQuality: "coarse",
      horizontalDistanceKm,
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: null,
      maxObstructionM: 0,
      blockingDistanceKm: null,
      blockingTerrainElevationM: null,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  const uncertaintyMarginM = Math.max(MIN_BLOCK_CONFIDENCE_MARGIN_M, altitudeUncertaintyM * 0.35);
  let minClearanceM = Infinity;
  let maxObstructionM = 0;
  let blockingDistanceKm: number | null = null;
  let blockingTerrainElevationM: number | null = null;

  for (const pt of points) {
    if (pt.elevationM === null || !Number.isFinite(pt.elevationM)) {
      return {
        status: "unknown",
        reasonCode: "TERRAIN_UNKNOWN_INCOMPLETE_PROFILE",
        explanation: "Terrain profile contains missing sample elevations.",
        terrainDataVersion: dataVersion,
        terrainQuality: "unreliable",
        horizontalDistanceKm,
        observerElevationM,
        aircraftAltitudeM,
        minClearanceM: null,
        maxObstructionM: 0,
        blockingDistanceKm: null,
        blockingTerrainElevationM: null,
        earthCurvatureDropAtTargetM: targetDropM,
        elevationAngleDeg,
        visualConditionsIncluded: false,
        scope: "geometric_terrain_and_earth_curvature_only",
      };
    }
    const dI = Math.max(0, Math.min(horizontalDistanceKm, pt.distanceKm));
    if (dI <= 0.05 || Math.abs(horizontalDistanceKm - dI) <= 0.05) {
      continue;
    }
    const frac = dI / Math.max(horizontalDistanceKm, 1e-6);
    const rayTangentM = obsEyeM + frac * (effectiveTargetM - obsEyeM);
    const dropIM = earthCurvatureDropM(dI, useRefraction);
    const rayLocalMslM = rayTangentM + dropIM;
    const clearanceM = rayLocalMslM - pt.elevationM;
    if (clearanceM < minClearanceM) {
      minClearanceM = clearanceM;
    }
    if (clearanceM < 0) {
      const obstruction = -clearanceM;
      if (obstruction > maxObstructionM) {
        maxObstructionM = obstruction;
        blockingDistanceKm = Number(dI.toFixed(2));
        blockingTerrainElevationM = Number(pt.elevationM.toFixed(1));
      }
    }
  }

  if (maxObstructionM >= uncertaintyMarginM) {
    return {
      status: "blocked",
      reasonCode: "TERRAIN_BLOCKED_VERIFIED_RIDGE",
      explanation: `Terrain ridge (${blockingTerrainElevationM}m MSL at ${blockingDistanceKm} km) blocks geometric line of sight by ${maxObstructionM.toFixed(1)}m (including Earth curvature drop).`,
      terrainDataVersion: dataVersion,
      terrainQuality: quality,
      horizontalDistanceKm: Number(horizontalDistanceKm.toFixed(2)),
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: Number(minClearanceM.toFixed(1)),
      maxObstructionM: Number(maxObstructionM.toFixed(1)),
      blockingDistanceKm,
      blockingTerrainElevationM,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  if (maxObstructionM > 0) {
    return {
      status: "unknown",
      reasonCode: "TERRAIN_UNKNOWN_GRAZING_WITHIN_UNCERTAINTY",
      explanation: `Sightline grazes terrain ridge within ${maxObstructionM.toFixed(1)}m vertical uncertainty margin; marked unknown rather than blocked.`,
      terrainDataVersion: dataVersion,
      terrainQuality: quality,
      horizontalDistanceKm: Number(horizontalDistanceKm.toFixed(2)),
      observerElevationM,
      aircraftAltitudeM,
      minClearanceM: Number(minClearanceM.toFixed(1)),
      maxObstructionM: Number(maxObstructionM.toFixed(1)),
      blockingDistanceKm,
      blockingTerrainElevationM,
      earthCurvatureDropAtTargetM: targetDropM,
      elevationAngleDeg,
      visualConditionsIncluded: false,
      scope: "geometric_terrain_and_earth_curvature_only",
    };
  }

  return {
    status: "clear",
    reasonCode: "TERRAIN_CLEAR_VERIFIED_PROFILE",
    explanation: `Geometric terrain line of sight is clear with ${Number.isFinite(minClearanceM) ? minClearanceM.toFixed(1) : "adequate"}m minimum ridge clearance (clouds, haze, and buildings not modeled).`,
    terrainDataVersion: dataVersion,
    terrainQuality: quality,
    horizontalDistanceKm: Number(horizontalDistanceKm.toFixed(2)),
    observerElevationM,
    aircraftAltitudeM,
    minClearanceM: Number.isFinite(minClearanceM) ? Number(minClearanceM.toFixed(1)) : null,
    maxObstructionM: 0,
    blockingDistanceKm: null,
    blockingTerrainElevationM: null,
    earthCurvatureDropAtTargetM: targetDropM,
    elevationAngleDeg,
    visualConditionsIncluded: false,
    scope: "geometric_terrain_and_earth_curvature_only",
  };
}

export function evaluateAirportArrivalV6(params: {
  aircraft: AircraftEvaluationInput;
  airport: AirportGeometryInput;
  observerLat: number;
  observerLon: number;
  alertRadiusKm: number;
  predictedCpaKm: number;
  timeToCpaS: number;
  radiusEntryS: number | null;
}): AirportArrivalEvaluation {
  const {
    aircraft,
    airport,
    observerLat,
    observerLon,
    alertRadiusKm,
    timeToCpaS,
    radiusEntryS,
  } = params;

  const airportDistKm = Number(
    haversineKm(aircraft.latitude, aircraft.longitude, airport.latitude, airport.longitude).toFixed(2),
  );
  const observerDistKm = haversineKm(aircraft.latitude, aircraft.longitude, observerLat, observerLon);
  const observerToAirportKm = haversineKm(observerLat, observerLon, airport.latitude, airport.longitude);
  const aglM =
    aircraft.altitudeM !== null ? Number((aircraft.altitudeM - airport.elevationM).toFixed(1)) : null;

  // Match runway thresholds
  let bestRw: {
    runwayId: string;
    distKm: number;
    alongKm: number;
    crossKm: number;
    headingDelta: number;
    alignedFinal: boolean;
    alignedIntercept: boolean;
    departing: boolean;
    thresholdLat: number;
    thresholdLon: number;
  } | null = null;

  for (const rw of airport.runways) {
    const distKm = haversineKm(aircraft.latitude, aircraft.longitude, rw.thresholdLat, rw.thresholdLon);
    const hdgDelta = Math.abs(angleDeltaDeg(aircraft.headingDeg, rw.headingDeg));
    const brgToThresh = bearingDeg(aircraft.latitude, aircraft.longitude, rw.thresholdLat, rw.thresholdLon);
    const inboundErr = Math.abs(angleDeltaDeg(brgToThresh, rw.headingDeg));
    const radErr = (inboundErr * Math.PI) / 180.0;
    const crossKm = Math.abs(distKm * Math.sin(radErr));
    const alongKm = distKm * Math.cos(radErr);

    const brgFromThresh = bearingDeg(rw.thresholdLat, rw.thresholdLon, aircraft.latitude, aircraft.longitude);
    const outboundErr = Math.abs(angleDeltaDeg(brgFromThresh, rw.headingDeg));
    const departing = hdgDelta <= 28.0 && outboundErr <= 30.0 && distKm <= 28.0;

    const alignedFinal =
      alongKm >= 0.2 && alongKm <= 38.0 && inboundErr <= 25.0 && crossKm <= 1.65 && hdgDelta <= 22.0;
    const alignedIntercept =
      !alignedFinal &&
      alongKm >= 0.5 &&
      alongKm <= 38.0 &&
      inboundErr <= 45.0 &&
      crossKm <= 4.2 &&
      hdgDelta <= 45.0;

    if (
      !bestRw ||
      (alignedFinal && !bestRw.alignedFinal) ||
      crossKm + 0.04 * hdgDelta < bestRw.crossKm + 0.04 * bestRw.headingDelta
    ) {
      bestRw = {
        runwayId: rw.runwayId,
        distKm: Number(distKm.toFixed(2)),
        alongKm: Number(alongKm.toFixed(2)),
        crossKm: Number(crossKm.toFixed(2)),
        headingDelta: Number(hdgDelta.toFixed(1)),
        alignedFinal,
        alignedIntercept,
        departing,
        thresholdLat: rw.thresholdLat,
        thresholdLon: rw.thresholdLon,
      };
    }
  }

  const destCode = (aircraft.destinationCode ?? "").toUpperCase().trim();
  const destMatches =
    destCode !== "" &&
    (destCode === airport.icao.toUpperCase() || destCode === airport.iata.toUpperCase());
  const descending = aircraft.verticalRateMps <= -0.8;
  const climbing = aircraft.verticalRateMps >= 1.8;

  const evidence: string[] = [];
  if (destMatches) evidence.push(`destination_matches:${airport.icao}`);
  if (bestRw?.alignedFinal) {
    evidence.push(
      `aligned_runway_threshold:${bestRw.runwayId}(xtk=${bestRw.crossKm}km,dist=${bestRw.distKm}km)`,
    );
  } else if (bestRw?.alignedIntercept) {
    evidence.push(`extended_centerline_intercept:${bestRw.runwayId}(xtk=${bestRw.crossKm}km)`);
  }
  if (descending) evidence.push(`sustained_descent(vr=${aircraft.verticalRateMps.toFixed(1)}mps)`);
  if (climbing) evidence.push(`sustained_climb(vr=${aircraft.verticalRateMps.toFixed(1)}mps)`);
  if (aglM !== null) evidence.push(`agl:${aglM.toFixed(0)}m`);

  const matchedGeometry = bestRw?.alignedFinal
    ? "runway_threshold"
    : bestRw?.alignedIntercept
      ? "extended_centerline"
      : airportDistKm <= 6.5
        ? "airport_boundary"
        : airportDistKm <= 85.0
          ? "terminal_corridor"
          : "none";

  const speedKmS = Math.max(0.03, aircraft.groundSpeedKts * 0.000514444);
  const landingTargetDistKm = bestRw ? bestRw.distKm : airportDistKm;
  const etaToLandingS = landingTargetDistKm / speedKmS;
  const candidateTimeS = radiusEntryS ?? timeToCpaS;
  const observerBeforeAirport =
    observerDistKm <= alertRadiusKm ||
    (candidateTimeS + 20.0 < etaToLandingS && observerDistKm < landingTargetDistKm);

  // 1. MISSED_APPROACH
  if (
    climbing &&
    aglM !== null &&
    aglM <= 2800 &&
    airportDistKm <= 22.0 &&
    (aircraft.previousLowApproach || destMatches)
  ) {
    evidence.push("missed_approach_or_go_around_climb");
    return {
      classification: "MISSED_APPROACH",
      matchedAirportIcao: airport.icao,
      matchedAirportIata: airport.iata,
      matchedAirportName: airport.name,
      matchedGeometry,
      matchedRunwayId: bestRw?.runwayId ?? "",
      airportDistanceKm: airportDistKm,
      thresholdDistanceKm: bestRw?.distKm ?? null,
      crossTrackKm: bestRw?.crossKm ?? null,
      alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
      headingDeltaDeg: bestRw?.headingDelta ?? null,
      aglM,
      observerBeforeAirport,
      suppressAlert: false,
      reasonCode: "AIRPORT_RELEASE_MISSED_APPROACH",
      explanation: `Missed approach / go-around detected near ${airport.icao} (climbing at ${aircraft.verticalRateMps.toFixed(1)} m/s); arrival suppression released.`,
      trackEvidence: evidence,
    };
  }

  // 2. HOLDING
  if ((aircraft.holdingScore ?? 0) >= 0.75) {
    evidence.push(`holding_loop(score=${(aircraft.holdingScore ?? 0).toFixed(2)})`);
    const shouldHold = observerDistKm > alertRadiusKm && !observerBeforeAirport;
    return {
      classification: "HOLDING",
      matchedAirportIcao: airport.icao,
      matchedAirportIata: airport.iata,
      matchedAirportName: airport.name,
      matchedGeometry: "terminal_corridor",
      matchedRunwayId: bestRw?.runwayId ?? "",
      airportDistanceKm: airportDistKm,
      thresholdDistanceKm: bestRw?.distKm ?? null,
      crossTrackKm: bestRw?.crossKm ?? null,
      alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
      headingDeltaDeg: bestRw?.headingDelta ?? null,
      aglM,
      observerBeforeAirport,
      suppressAlert: shouldHold,
      reasonCode: shouldHold ? "AIRPORT_SUPPRESS_HOLDING_PATTERN" : "AIRPORT_HOLDING_NEAR_OBSERVER",
      explanation: `Aircraft is in a multi-sample terminal holding orbit near ${airport.icao}; temporary straight-line tangent does not represent an inbound pass.`,
      trackEvidence: evidence,
    };
  }

  // 3. DEPARTURE
  if (climbing && airportDistKm <= 45.0 && !destMatches && (bestRw?.departing || airportDistKm <= 18.0)) {
    evidence.push("climbing_departure_away_from_airport");
    return {
      classification: "DEPARTURE",
      matchedAirportIcao: airport.icao,
      matchedAirportIata: airport.iata,
      matchedAirportName: airport.name,
      matchedGeometry,
      matchedRunwayId: bestRw?.runwayId ?? "",
      airportDistanceKm: airportDistKm,
      thresholdDistanceKm: bestRw?.distKm ?? null,
      crossTrackKm: bestRw?.crossKm ?? null,
      alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
      headingDeltaDeg: bestRw?.headingDelta ?? null,
      aglM,
      observerBeforeAirport: true,
      suppressAlert: false,
      reasonCode: "AIRPORT_ALLOW_DEPARTURE",
      explanation: `Aircraft is departing and climbing away from ${airport.icao} (runway ${bestRw?.runwayId || "terminal"}); live observer trajectory remains authoritative.`,
      trackEvidence: evidence,
    };
  }

  // 4. OVERFLIGHT
  if (!destMatches && !descending && ((aglM !== null && aglM >= 3200.0) || !(bestRw?.alignedFinal || bestRw?.alignedIntercept))) {
    evidence.push("level_or_high_altitude_overflight");
    return {
      classification: "OVERFLIGHT",
      matchedAirportIcao: airport.icao,
      matchedAirportIata: airport.iata,
      matchedAirportName: airport.name,
      matchedGeometry,
      matchedRunwayId: bestRw?.runwayId ?? "",
      airportDistanceKm: airportDistKm,
      thresholdDistanceKm: bestRw?.distKm ?? null,
      crossTrackKm: bestRw?.crossKm ?? null,
      alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
      headingDeltaDeg: bestRw?.headingDelta ?? null,
      aglM,
      observerBeforeAirport,
      suppressAlert: false,
      reasonCode: "AIRPORT_ALLOW_OVERFLIGHT",
      explanation: `Aircraft is transiting/overflying near ${airport.icao} without descent or runway-threshold arrival alignment; live trajectory remains authoritative.`,
      trackEvidence: evidence,
    };
  }

  // 5. ARRIVAL vs AIRPORT_NEARBY (pass-before-airport protection)
  const hasRunwayFinal = Boolean(
    bestRw && (bestRw.alignedFinal || bestRw.alignedIntercept) && descending && (aglM === null || aglM <= 4200.0),
  );
  const hasTerminalArrival = Boolean(
    destMatches && airportDistKm <= 180.0 && (descending || (aglM !== null && aglM <= 8500.0)) && !climbing,
  );

  if (hasRunwayFinal || hasTerminalArrival) {
    if (observerDistKm <= alertRadiusKm || observerToAirportKm <= alertRadiusKm) {
      return {
        classification: "ARRIVAL",
        matchedAirportIcao: airport.icao,
        matchedAirportIata: airport.iata,
        matchedAirportName: airport.name,
        matchedGeometry,
        matchedRunwayId: bestRw?.runwayId ?? "",
        airportDistanceKm: airportDistKm,
        thresholdDistanceKm: bestRw?.distKm ?? null,
        crossTrackKm: bestRw?.crossKm ?? null,
        alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
        headingDeltaDeg: bestRw?.headingDelta ?? null,
        aglM,
        observerBeforeAirport: true,
        suppressAlert: false,
        reasonCode: "AIRPORT_ARRIVAL_INSIDE_OBSERVER_RADIUS",
        explanation: `Aircraft is arriving at ${airport.icao} within the observer alert radius; genuine nearby pass is preserved.`,
        trackEvidence: [...evidence, "observer_radius_includes_arrival"],
      };
    }

    if (!observerBeforeAirport && etaToLandingS + 15.0 < candidateTimeS) {
      return {
        classification: "ARRIVAL",
        matchedAirportIcao: airport.icao,
        matchedAirportIata: airport.iata,
        matchedAirportName: airport.name,
        matchedGeometry,
        matchedRunwayId: bestRw?.runwayId ?? "",
        airportDistanceKm: airportDistKm,
        thresholdDistanceKm: bestRw?.distKm ?? null,
        crossTrackKm: bestRw?.crossKm ?? null,
        alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
        headingDeltaDeg: bestRw?.headingDelta ?? null,
        aglM,
        observerBeforeAirport: false,
        suppressAlert: true,
        reasonCode: "AIRPORT_SUPPRESS_VERIFIED_ARRIVAL",
        explanation: `Verified arrival at ${airport.icao} Runway ${bestRw?.runwayId || "approach"} (${bestRw?.distKm ?? airportDistKm} km to threshold, xtk ${bestRw?.crossKm ?? 0} km): lands before reaching observer radius.`,
        trackEvidence: evidence,
      };
    }

    return {
      classification: "AIRPORT_NEARBY",
      matchedAirportIcao: airport.icao,
      matchedAirportIata: airport.iata,
      matchedAirportName: airport.name,
      matchedGeometry,
      matchedRunwayId: bestRw?.runwayId ?? "",
      airportDistanceKm: airportDistKm,
      thresholdDistanceKm: bestRw?.distKm ?? null,
      crossTrackKm: bestRw?.crossKm ?? null,
      alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
      headingDeltaDeg: bestRw?.headingDelta ?? null,
      aglM,
      observerBeforeAirport: true,
      suppressAlert: false,
      reasonCode: "AIRPORT_ALLOW_PASS_BEFORE_LANDING",
      explanation: `Arrival toward ${airport.icao} Runway ${bestRw?.runwayId || "approach"} genuinely passes the observer before reaching the runway threshold; alert is preserved.`,
      trackEvidence: [...evidence, "genuine_pass_before_airport"],
    };
  }

  return {
    classification: "AIRPORT_NEARBY",
    matchedAirportIcao: airport.icao,
    matchedAirportIata: airport.iata,
    matchedAirportName: airport.name,
    matchedGeometry,
    matchedRunwayId: bestRw?.runwayId ?? "",
    airportDistanceKm: airportDistKm,
    thresholdDistanceKm: bestRw?.distKm ?? null,
    crossTrackKm: bestRw?.crossKm ?? null,
    alongTrackBeforeThresholdKm: bestRw?.alongKm ?? null,
    headingDeltaDeg: bestRw?.headingDelta ?? null,
    aglM,
    observerBeforeAirport,
    suppressAlert: false,
    reasonCode: "AIRPORT_ALLOW_INSUFFICIENT_ARRIVAL_EVIDENCE",
    explanation: `Traffic near ${airport.icao} lacks sufficient runway-threshold or terminal-arrival evidence to suppress a qualifying observer pass.`,
    trackEvidence: evidence,
  };
}

export function refineAgeAwareRadiusEntryS(
  pathSamples: ReadonlyArray<{ seconds: number; horizontalKm: number }>,
  alertRadiusKm: number,
  fallbackEntryS: number | null = null,
): number | null {
  if (!pathSamples.length) return fallbackEntryS;
  if (pathSamples[0].horizontalKm <= alertRadiusKm) {
    return Math.max(0, Number(pathSamples[0].seconds.toFixed(3)));
  }
  for (let i = 0; i < pathSamples.length - 1; i++) {
    const p0 = pathSamples[i];
    const p1 = pathSamples[i + 1];
    if (p0.horizontalKm > alertRadiusKm && p1.horizontalKm <= alertRadiusKm) {
      const dh = p0.horizontalKm - p1.horizontalKm;
      const dt = p1.seconds - p0.seconds;
      if (dh > 1e-6 && dt >= 0) {
        const frac = Math.max(0, Math.min(1, (p0.horizontalKm - alertRadiusKm) / dh));
        return Number(Math.max(0, p0.seconds + frac * dt).toFixed(3));
      }
      return Number(Math.max(0, p1.seconds).toFixed(3));
    }
  }
  return fallbackEntryS;
}

export function diagnoseEtaCpaV6(params: {
  observationAgeS: number | null;
  groundSpeedKts: number;
  timeToCpaS: number | null;
  radiusEntryS: number | null;
  turnRateDegS?: number;
  airportClassification?: AirportClassificationV6;
  airportAlertChanged?: boolean;
  deliveryLagS?: number;
}): EtaCpaEvaluation {
  const {
    observationAgeS,
    groundSpeedKts,
    timeToCpaS,
    radiusEntryS,
    turnRateDegS = 0,
    airportClassification = "NONE",
    airportAlertChanged = false,
    deliveryLagS = 0,
  } = params;

  const effectiveAgeS = observationAgeS !== null && Number.isFinite(observationAgeS) ? Math.max(0, observationAgeS) : Infinity;
  const freshnessLimitS = groundSpeedKts >= 350 ? 15.0 : groundSpeedKts >= 180 ? 22.0 : 30.0;
  const stale = !Number.isFinite(effectiveAgeS) || effectiveAgeS > 65.0;
  const speedKmS = Math.max(0.03, groundSpeedKts * 0.000514444);
  const posUncKm = Number.isFinite(effectiveAgeS) ? Math.min(4.5, 0.15 + effectiveAgeS * speedKmS * 0.45) : 5.0;
  const horizonS = timeToCpaS ?? 0;
  const turnCrossKm = (horizonS / 60.0) * Math.abs(turnRateDegS) * 0.45;
  const cpaUncertaintyKm = Number((posUncKm + turnCrossKm).toFixed(3));
  const etaUncertaintyS =
    timeToCpaS !== null && !stale
      ? Number(
          Math.max(
            3.0,
            posUncKm / speedKmS + (Math.abs(turnRateDegS) >= 0.25 ? 0.08 * horizonS : 0.03 * horizonS),
          ).toFixed(2),
        )
      : null;

  let primaryDriver: EtaRootCauseDriverV6 = "STABLE_NOMINAL";
  let reasonCode = "ETA_DRIVER_STABLE_MIDPOINT_TRACK";
  let explanation = "Fresh multi-sample track with stable heading and groundspeed; age-aware midpoint ETA is nominal.";

  if (stale || effectiveAgeS >= Math.max(8.0, freshnessLimitS * 0.7)) {
    primaryDriver = "OLD_OBSERVATIONS";
    reasonCode = "ETA_DRIVER_OBSERVATION_AGE";
    explanation = `Observation age is ${Number.isFinite(effectiveAgeS) ? effectiveAgeS.toFixed(1) : "missing"}s (freshness limit ${freshnessLimitS.toFixed(1)}s); age-aware midpoint ETA compensation applied.`;
  } else if (
    airportAlertChanged ||
    airportClassification === "ARRIVAL" ||
    airportClassification === "HOLDING" ||
    airportClassification === "MISSED_APPROACH"
  ) {
    primaryDriver = "AIRPORT_PATH_HANDLING";
    reasonCode = `ETA_DRIVER_AIRPORT_${airportClassification}`;
    explanation = `Terminal airport path handling (${airportClassification}) governs expected trajectory and overrides cruise extrapolation.`;
  } else if (Math.abs(turnRateDegS) >= 0.2) {
    primaryDriver = "CHANGING_TRAJECTORY";
    reasonCode = "ETA_DRIVER_MANEUVERING_TRACK";
    explanation = `Aircraft trajectory is actively turning (${turnRateDegS.toFixed(2)} deg/s); decaying midpoint integration bounds extrapolation.`;
  } else if (deliveryLagS >= 4.0) {
    primaryDriver = "DELIVERY_TIMING";
    reasonCode = "ETA_DRIVER_CONFIRMATION_OR_DELIVERY_TIMING";
    explanation = `Notification timing reflects confirmation gating or delivery latency (${deliveryLagS.toFixed(1)}s).`;
  }

  const etaAvailable = timeToCpaS !== null && !stale;
  const etaReliable = Boolean(
    etaAvailable && etaUncertaintyS !== null && etaUncertaintyS <= Math.max(45.0, horizonS * 0.35),
  );

  return {
    primaryDriver,
    reasonCode,
    explanation,
    effectiveAgeS: Number.isFinite(effectiveAgeS) ? Number(effectiveAgeS.toFixed(2)) : 999,
    freshnessLimitS,
    ageCorrectedTimeToCpaS: timeToCpaS !== null && Number.isFinite(effectiveAgeS) ? Number(Math.max(0, timeToCpaS - effectiveAgeS).toFixed(2)) : null,
    ageCorrectedRadiusEntryS: radiusEntryS !== null && Number.isFinite(effectiveAgeS) ? Number(Math.max(0, radiusEntryS - effectiveAgeS).toFixed(2)) : null,
    etaUncertaintyS,
    cpaUncertaintyKm,
    etaAvailable,
    etaReliable,
  };
}

export function triageFreshFailureV6(params: {
  caseId: string;
  mainSha?: string;
  observationAgeS?: number;
  conflictingSources?: boolean;
  missingCoverage?: boolean;
  duplicateNotification?: boolean;
  wronglyCancelled?: boolean;
  terrainBlockedPass?: boolean;
  airportMisclassified?: boolean;
  etaDriftOrTurn?: boolean;
  commitBehavior: string;
  expectedResult: string;
}): {
  caseId: string;
  mainSha: string;
  problemClass: TriageProblemClassV6;
  reasonCode: string;
  commitBehavior: string;
  expectedResult: string;
  verifiedFromFreshObservation: true;
} {
  let problemClass: TriageProblemClassV6 = "ETA_TRAJECTORY";
  let reasonCode = "TRAJECTORY_ETA_CPA_DRIFT";

  if (params.missingCoverage || params.conflictingSources || (params.observationAgeS ?? 0) > 45.0) {
    problemClass = "DATA_FRESHNESS";
    reasonCode = "FRESHNESS_STALE_OR_CONFLICTING_ADSB";
  } else if (params.duplicateNotification || params.wronglyCancelled) {
    problemClass = "NOTIFICATION_LIFECYCLE";
    reasonCode = "LIFECYCLE_DELIVERY_OR_CANCELLATION_DEFECT";
  } else if (params.terrainBlockedPass) {
    problemClass = "TERRAIN_VISIBILITY";
    reasonCode = "TERRAIN_GEOMETRIC_OBSTRUCTION_DEFECT";
  } else if (params.airportMisclassified) {
    problemClass = "AIRPORT_LOGIC";
    reasonCode = "AIRPORT_TERMINAL_CLASSIFICATION_DEFECT";
  } else if (params.etaDriftOrTurn) {
    problemClass = "ETA_TRAJECTORY";
    reasonCode = "TRAJECTORY_ETA_CPA_DRIFT";
  }

  return {
    caseId: params.caseId,
    mainSha: params.mainSha ?? DEFAULT_PINNED_MAIN_SHA,
    problemClass,
    reasonCode,
    commitBehavior: params.commitBehavior,
    expectedResult: params.expectedResult,
    verifiedFromFreshObservation: true,
  };
}
