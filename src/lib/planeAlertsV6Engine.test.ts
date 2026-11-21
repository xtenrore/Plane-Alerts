import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  VERSION,
  PREDICTION_VERSION,
  DEFAULT_PINNED_MAIN_SHA,
  ROLLBACK_TARGET_RELEASE,
  COMMUNITY_ROLLBACK_RELEASE,
  anonymizeObserverRef,
  redactSensitiveConfig,
  classifyObservationFreshness,
  earthCurvatureDropM,
  evaluateTerrainLineOfSightV6,
  evaluateAirportArrivalV6,
  refineAgeAwareRadiusEntryS,
  diagnoseEtaCpaV6,
  triageFreshFailureV6,
  type AirportGeometryInput,
} from "./planeAlertsV6Engine.ts";

const sampleLtfmAirport: AirportGeometryInput = {
  icao: "LTFM",
  iata: "IST",
  name: "Istanbul Airport",
  latitude: 41.275278,
  longitude: 28.751944,
  elevationM: 99.0,
  runways: [
    {
      runwayId: "17L",
      thresholdLat: 41.293,
      thresholdLon: 28.741,
      headingDeg: 180.0,
      oppositeLat: 41.256,
      oppositeLon: 28.741,
    },
  ],
};

describe("Plane Alerts v6.0.0 — Main-Based Diagnostic and Fix Plan Engine", () => {
  it("Phase 0 & 1: pins v6.0.0 release identity, redacts secrets/coordinates, and classifies missing coverage as unknown", () => {
    assert.equal(VERSION, "6.0.0");
    assert.equal(PREDICTION_VERSION, "5.3-3d-proximity-age-aware");
    assert.equal(ROLLBACK_TARGET_RELEASE, "v5.8.8");
    assert.equal(COMMUNITY_ROLLBACK_RELEASE, "v5.5.3");

    const ref = anonymizeObserverRef(41.0422, 28.9861, "user_42");
    assert.ok(ref.startsWith("obs_"));
    assert.ok(!ref.includes("41.0422"));

    const redacted = redactSensitiveConfig({
      DATABASE_BACKEND: "sqlite",
      TELEGRAM_BOT_TOKEN: "12345:secret",
      OBSERVER_LAT: 41.0422,
      OBSERVER_LON: 28.9861,
    });
    assert.equal(redacted.DATABASE_BACKEND, "sqlite");
    assert.equal(redacted.TELEGRAM_BOT_TOKEN, "<REDACTED>");
    assert.equal(redacted.OBSERVER_LAT, "<REDACTED>");

    const stale = classifyObservationFreshness(85.0, true);
    assert.equal(stale.freshnessStatus, "stale");
    assert.equal(stale.coverageStatus, "unknown");

    const missing = classifyObservationFreshness(null, false);
    assert.equal(missing.freshnessStatus, "missing_unknown");
    assert.equal(missing.coverageStatus, "unknown");
  });

  it("Phase 2: triages fresh failures into 5 distinct problem classes", () => {
    assert.equal(
      triageFreshFailureV6({
        caseId: "c1",
        observationAgeS: 72,
        commitBehavior: "stale sample",
        expectedResult: "unknown coverage",
      }).problemClass,
      "DATA_FRESHNESS",
    );
    assert.equal(
      triageFreshFailureV6({
        caseId: "c2",
        etaDriftOrTurn: true,
        commitBehavior: "base turn lag",
        expectedResult: "turn damping",
      }).problemClass,
      "ETA_TRAJECTORY",
    );
    assert.equal(
      triageFreshFailureV6({
        caseId: "c3",
        airportMisclassified: true,
        commitBehavior: "arrival false alert",
        expectedResult: "runway threshold suppression",
      }).problemClass,
      "AIRPORT_LOGIC",
    );
    assert.equal(
      triageFreshFailureV6({
        caseId: "c4",
        terrainBlockedPass: true,
        commitBehavior: "hidden behind ridge",
        expectedResult: "terrain blocked",
      }).problemClass,
      "TERRAIN_VISIBILITY",
    );
    assert.equal(
      triageFreshFailureV6({
        caseId: "c5",
        duplicateNotification: true,
        commitBehavior: "duplicate send",
        expectedResult: "deduplicate lifecycle",
      }).problemClass,
      "NOTIFICATION_LIFECYCLE",
    );
  });

  it("Phase 3A: suppresses verified runway-threshold arrival before observer but preserves genuine pass-before-runway and missed approach", () => {
    // 1. Final approach to Runway 17L landing before reaching observer south of airport -> suppressed
    const arrivalBeforeObserver = evaluateAirportArrivalV6({
      aircraft: {
        icao24: "4BB002",
        callsign: "THY202",
        latitude: 41.335,
        longitude: 28.741,
        altitudeM: 520,
        groundSpeedKts: 142,
        headingDeg: 180,
        verticalRateMps: -3.8,
        positionAgeS: 1.5,
        source: "airplanes.live",
        destinationCode: "LTFM",
      },
      airport: sampleLtfmAirport,
      observerLat: 41.18,
      observerLon: 28.741,
      alertRadiusKm: 6.0,
      predictedCpaKm: 2.1,
      timeToCpaS: 230,
      radiusEntryS: 200,
    });
    assert.equal(arrivalBeforeObserver.classification, "ARRIVAL");
    assert.equal(arrivalBeforeObserver.matchedRunwayId, "17L");
    assert.equal(arrivalBeforeObserver.suppressAlert, true);

    // 2. Observer lies NORTH of Runway 17L threshold so aircraft passes observer BEFORE landing -> NOT suppressed
    const passBeforeRunway = evaluateAirportArrivalV6({
      aircraft: {
        icao24: "4BB003",
        callsign: "THY303",
        latitude: 41.36,
        longitude: 28.741,
        altitudeM: 720,
        groundSpeedKts: 150,
        headingDeg: 180,
        verticalRateMps: -3.2,
        positionAgeS: 1.2,
        source: "airplanes.live",
        destinationCode: "LTFM",
      },
      airport: sampleLtfmAirport,
      observerLat: 41.34,
      observerLon: 28.741,
      alertRadiusKm: 6.0,
      predictedCpaKm: 0.4,
      timeToCpaS: 22,
      radiusEntryS: 10,
    });
    assert.equal(passBeforeRunway.observerBeforeAirport, true);
    assert.equal(passBeforeRunway.suppressAlert, false);

    // 3. Missed approach / go-around climb releases suppression
    const goAround = evaluateAirportArrivalV6({
      aircraft: {
        icao24: "4BB004",
        callsign: "THY404",
        latitude: 41.27,
        longitude: 28.741,
        altitudeM: 620,
        groundSpeedKts: 175,
        headingDeg: 180,
        verticalRateMps: 7.2,
        positionAgeS: 1.0,
        source: "airplanes.live",
        destinationCode: "LTFM",
        previousLowApproach: true,
      },
      airport: sampleLtfmAirport,
      observerLat: 41.21,
      observerLon: 28.741,
      alertRadiusKm: 8.0,
      predictedCpaKm: 1.2,
      timeToCpaS: 75,
      radiusEntryS: 50,
    });
    assert.equal(goAround.classification, "MISSED_APPROACH");
    assert.equal(goAround.suppressAlert, false);
  });

  it("Phase 3B: computes curvature-aware terrain LOS and returns unknown for missing or coarse DEM data", () => {
    const drop10Km = earthCurvatureDropM(10.0);
    assert.ok(drop10Km > 5.5 && drop10Km < 6.3);

    const blocked = evaluateTerrainLineOfSightV6({
      observerElevationM: 50,
      aircraftAltitudeM: 420,
      horizontalDistanceKm: 10.0,
      profile: {
        dataVersion: "SRTM_30M_V6",
        quality: "verified",
        resolutionM: 30,
        points: [
          { distanceKm: 2.0, elevationM: 90 },
          { distanceKm: 4.5, elevationM: 380 },
          { distanceKm: 6.8, elevationM: 150 },
          { distanceKm: 8.5, elevationM: 110 },
        ],
      },
    });
    assert.equal(blocked.status, "blocked");
    assert.equal(blocked.blockingTerrainElevationM, 380);
    assert.equal(blocked.visualConditionsIncluded, false);

    const coarse = evaluateTerrainLineOfSightV6({
      observerElevationM: 50,
      aircraftAltitudeM: 420,
      horizontalDistanceKm: 10.0,
      profile: {
        dataVersion: "ETOPO_1KM",
        quality: "verified",
        resolutionM: 1000,
        points: [
          { distanceKm: 2.5, elevationM: 380 },
          { distanceKm: 5.0, elevationM: 390 },
        ],
      },
    });
    assert.equal(coarse.status, "unknown");
    assert.equal(coarse.terrainQuality, "coarse");
  });

  it("Phase 3C: interpolates sub-step radius entry and attributes ETA drivers accurately", () => {
    const entry = refineAgeAwareRadiusEntryS(
      [
        { seconds: 0, horizontalKm: 10.0 },
        { seconds: 20, horizontalKm: 9.0 },
        { seconds: 40, horizontalKm: 7.0 },
      ],
      8.0,
    );
    assert.equal(entry, 30.0);

    const oldDiag = diagnoseEtaCpaV6({
      observationAgeS: 26.0,
      groundSpeedKts: 240,
      timeToCpaS: 60.0,
      radiusEntryS: 30.0,
    });
    assert.equal(oldDiag.primaryDriver, "OLD_OBSERVATIONS");

    const turnDiag = diagnoseEtaCpaV6({
      observationAgeS: 2.0,
      groundSpeedKts: 240,
      timeToCpaS: 60.0,
      radiusEntryS: 30.0,
      turnRateDegS: 1.85,
    });
    assert.equal(turnDiag.primaryDriver, "CHANGING_TRAJECTORY");
  });
});
