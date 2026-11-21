import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  VERSION,
  PREDICTION_VERSION,
  DEFAULT_PINNED_MAIN_SHA,
  ROLLBACK_TARGET_RELEASE,
  COMMUNITY_ROLLBACK_RELEASE,
  USER_FEEDBACK_CATEGORIES_V6,
  anonymizeObserverRef,
  redactSensitiveConfig,
  classifyObservationFreshness,
  evaluateTerrainLineOfSightV6,
  evaluateAirportArrivalV6,
  diagnoseEtaCpaV6,
  triageFreshFailureV6,
} from "./src/lib/planeAlertsV6Engine.ts";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PORT = Number(process.env.PORT || 3000);

const indexHtmlPath = path.join(__dirname, "index.html");

const server = http.createServer((req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  if (url.pathname === "/health" || url.pathname === "/ready") {
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify({
        status: "ok",
        ready: true,
        version: VERSION,
        prediction_version: PREDICTION_VERSION,
        pinned_main_sha: DEFAULT_PINNED_MAIN_SHA,
        rollback_target_release: ROLLBACK_TARGET_RELEASE,
        community_rollback_release: COMMUNITY_ROLLBACK_RELEASE,
      }),
    );
    return;
  }

  if (url.pathname === "/api/v6/status") {
    const sampleTerrain = evaluateTerrainLineOfSightV6({
      observerElevationM: 85,
      aircraftAltitudeM: 1050,
      horizontalDistanceKm: 10,
      profile: {
        dataVersion: "SRTM_30M_V6",
        quality: "verified",
        resolutionM: 30,
        points: [
          { distanceKm: 2.0, elevationM: 90 },
          { distanceKm: 4.5, elevationM: 95 },
          { distanceKm: 7.0, elevationM: 110 },
        ],
      },
    });
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify({
        version: VERSION,
        predictionVersion: PREDICTION_VERSION,
        pinnedMainSha: DEFAULT_PINNED_MAIN_SHA,
        rollbackTargetRelease: ROLLBACK_TARGET_RELEASE,
        communityRollbackRelease: COMMUNITY_ROLLBACK_RELEASE,
        inheritedArtifactsUnverified: true,
        feedbackCategories: USER_FEEDBACK_CATEGORIES_V6,
        sampleObserverRef: anonymizeObserverRef(41.0422, 28.9861, "user_42"),
        sampleRedactedConfig: redactSensitiveConfig({
          DATABASE_BACKEND: "sqlite",
          TELEGRAM_BOT_TOKEN: "redacted",
          OBSERVER_LAT: 41.0422,
        }),
        sampleFreshness: classifyObservationFreshness(2.1, true),
        sampleTerrain,
        sampleTriage: triageFreshFailureV6({
          caseId: "live_check_1",
          airportMisclassified: true,
          commitBehavior: "Evaluated runway 17L final approach",
          expectedResult: "AIRPORT_SUPPRESS_VERIFIED_ARRIVAL",
        }),
      }),
    );
    return;
  }

  if (url.pathname === "/" || url.pathname === "/index.html" || url.pathname === "/next60") {
    const html = fs.readFileSync(indexHtmlPath, "utf-8");
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(html);
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({ error: "Not found" }));
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`Plane Alerts v${VERSION} Diagnostic Workbench listening on http://0.0.0.0:${PORT}`);
});
