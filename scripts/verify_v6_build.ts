import { execFileSync } from "node:child_process";
import assert from "node:assert/strict";
import fs from "node:fs";
import {
  VERSION,
  PREDICTION_VERSION,
  DEFAULT_PINNED_MAIN_SHA,
  ROLLBACK_TARGET_RELEASE,
  COMMUNITY_ROLLBACK_RELEASE,
  evaluateTerrainLineOfSightV6,
  evaluateAirportArrivalV6,
  diagnoseEtaCpaV6,
} from "../src/lib/planeAlertsV6Engine.ts";

assert.equal(VERSION, "6.0.0");
assert.equal(PREDICTION_VERSION, "5.3-3d-proximity-age-aware");
assert.equal(ROLLBACK_TARGET_RELEASE, "v5.8.8");
assert.equal(COMMUNITY_ROLLBACK_RELEASE, "v5.5.3");
assert.ok(DEFAULT_PINNED_MAIN_SHA.length >= 7);

for (const requiredFile of [
  "metadata.json",
  "index.html",
  "server.ts",
  "app/version.py",
  "app/intelligence/diagnostics_v6.py",
  "app/intelligence/airport_arrival_v6.py",
  "app/intelligence/terrain_visibility_v6.py",
  "app/intelligence/eta_cpa_v6.py",
  "docs/releases/v6.0.0.md",
  "requirements.lock",
]) {
  assert.ok(fs.existsSync(requiredFile), `Missing required file: ${requiredFile}`);
}

const losCheck = evaluateTerrainLineOfSightV6({
  observerElevationM: 50,
  aircraftAltitudeM: 420,
  horizontalDistanceKm: 10,
  profile: {
    dataVersion: "SRTM_30M_V6",
    quality: "verified",
    resolutionM: 30,
    points: [
      { distanceKm: 2.0, elevationM: 90 },
      { distanceKm: 4.5, elevationM: 380 },
      { distanceKm: 7.0, elevationM: 120 },
    ],
  },
});
assert.equal(losCheck.status, "blocked");

const airportCheck = evaluateAirportArrivalV6({
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
  airport: {
    icao: "LTFM",
    iata: "IST",
    name: "Istanbul Airport",
    latitude: 41.275278,
    longitude: 28.751944,
    elevationM: 99,
    runways: [
      {
        runwayId: "17L",
        thresholdLat: 41.293,
        thresholdLon: 28.741,
        headingDeg: 180,
        oppositeLat: 41.256,
        oppositeLon: 28.741,
      },
    ],
  },
  observerLat: 41.18,
  observerLon: 28.741,
  alertRadiusKm: 6.0,
  predictedCpaKm: 2.1,
  timeToCpaS: 230,
  radiusEntryS: 200,
});
assert.equal(airportCheck.classification, "ARRIVAL");
assert.equal(airportCheck.suppressAlert, true);

const etaCheck = diagnoseEtaCpaV6({
  observationAgeS: 2.0,
  groundSpeedKts: 210,
  timeToCpaS: 68.0,
  radiusEntryS: 42.0,
});
assert.equal(etaCheck.primaryDriver, "STABLE_NOMINAL");

execFileSync("python3", ["-m", "compileall", "-q", "app", "vercel_runtime", "worker.py", "tests"], {
  stdio: "inherit",
});

console.log("Plane Alerts v6.0.0 TypeScript & Python verification passed.");
