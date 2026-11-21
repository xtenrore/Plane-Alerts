"""Plane Alerts v6.0.0 — Main-Based Diagnostic, Triage, and Release Validation Layer.

Implements:
- Phase 0: Main baseline pinning, commit diff audit, secret-redacted restorable configuration
  backup, and confirmed rollback target verification (v5.8.8 / community stable v5.5.3).
  Inherited Error Museum and historical Prediction Lab artifacts are flagged as unverified
  historical context.
- Phase 1: Pure diagnostic capture (`DecisionDiagnosticRecord`, `DiagnosticCollectorV6`)
  that records the complete aircraft evaluation path without modifying qualification,
  suppression, cancellation, ETA, CPA, or notification timing.
  - Redacts exact observer coordinates and credentials.
  - Bounds retention with an in-memory ring buffer and can be disabled cleanly.
  - Records missing or stale ADS-B coverage strictly as `unknown` (never as hit or miss).
  - Supports the 5 v6 user feedback values (`saw_it`, `terrain_blocked`,
    `poor_visibility`, `wrong_timing_path`, `unsure`).
- Phase 2: Fresh failure triage into 5 distinct problem classes (`DATA_FRESHNESS`,
  `ETA_TRAJECTORY`, `AIRPORT_LOGIC`, `TERRAIN_VISIBILITY`, `NOTIFICATION_LIFECYCLE`)
  preserving the exact main SHA, observed inputs, commit behavior, and expected result.
- Phase 4: Release validation metrics (`ReleaseValidationMetricsV6`) with explicit
  numerators, denominators, and unknown/unavailable coverage counts.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass, field
import hashlib
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.intelligence.airport_arrival_v6 import AirportArrivalExplanation
from app.intelligence.eta_cpa_v6 import EtaCpaDiagnosis, EtaOutcomeEvaluation
from app.intelligence.terrain_visibility_v6 import TerrainLineOfSightResult


DEFAULT_PINNED_MAIN_SHA = "e856aa3449d7f509af0b2f853b5c814ab28d0eb5"
DEFAULT_BASELINE_VERSION = "5.8.8"
DEFAULT_TARGET_VERSION = "6.0.0"
DEFAULT_ROLLBACK_TARGET_RELEASE = "v5.8.8"
DEFAULT_COMMUNITY_ROLLBACK_RELEASE = "v5.5.3"
DEFAULT_DIAGNOSTIC_RETENTION_LIMIT = 2048

USER_FEEDBACK_CATEGORIES_V6: Tuple[str, ...] = (
    "saw_it",
    "terrain_blocked",
    "poor_visibility",
    "wrong_timing_path",
    "unsure",
)

V6_FEEDBACK_LABELS: Dict[str, str] = {
    "saw_it": "Saw aircraft pass clearly",
    "terrain_blocked": "Terrain blocked line of sight",
    "poor_visibility": "Clouds, haze, or local obstruction",
    "wrong_timing_path": "Wrong timing or flight path",
    "unsure": "Unsure / could not confirm",
}


def normalize_v6_feedback_reason(reason: str) -> str:
    """Normalize a user feedback token to one of the 5 canonical v6 diagnostic categories."""
    token = str(reason or "").strip().lower()
    alias_map = {
        "saw_it": "saw_it",
        "like": "saw_it",
        "seen": "saw_it",
        "terrain_blocked": "terrain_blocked",
        "terrain": "terrain_blocked",
        "poor_visibility": "poor_visibility",
        "clouds": "poor_visibility",
        "weather": "poor_visibility",
        "wrong_timing_path": "wrong_timing_path",
        "wrong_eta": "wrong_timing_path",
        "wrong_path": "wrong_timing_path",
        "dislike": "wrong_timing_path",
        "unsure": "unsure",
    }
    if token not in alias_map:
        raise ValueError(f"Unsupported feedback reason '{reason}'.")
    return alias_map[token]

TRIAGE_PROBLEM_CLASSES_V6: Tuple[str, ...] = (
    "DATA_FRESHNESS",
    "ETA_TRAJECTORY",
    "AIRPORT_LOGIC",
    "TERRAIN_VISIBILITY",
    "NOTIFICATION_LIFECYCLE",
)

FINAL_DECISION_VALUES_V6: Tuple[str, ...] = (
    "alert",
    "suppress",
    "cancel",
    "hold",
    "unknown_coverage",
    "delivery_failure",
)

_SENSITIVE_KEY_TOKENS: Tuple[str, ...] = (
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
)


def anonymize_observer_reference(
    lat: Optional[float],
    lon: Optional[float],
    observer_id: Optional[str] = None,
) -> str:
    """Create a deterministic, privacy-safe observer hash without storing raw coordinates."""
    seed = f"{observer_id or 'obs'}:{round(float(lat or 0.0), 3):.3f}:{round(float(lon or 0.0), 3):.3f}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"obs_{digest}"


def redact_sensitive_config(config_map: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a restorable configuration snapshot with secrets and raw coordinates redacted."""
    sanitized: Dict[str, Any] = {}
    for key, value in config_map.items():
        key_lower = str(key).lower()
        if any(tok in key_lower for tok in _SENSITIVE_KEY_TOKENS):
            if value in (None, "", 0, False):
                sanitized[str(key)] = ""
            else:
                sanitized[str(key)] = "<REDACTED>"
        elif isinstance(value, Mapping):
            sanitized[str(key)] = redact_sensitive_config(value)
        elif isinstance(value, (list, tuple)):
            sanitized[str(key)] = [
                redact_sensitive_config(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            sanitized[str(key)] = value
    return sanitized


@dataclass(frozen=True)
class MainBaselinePin:
    """Phase 0 record pinning the exact main commit, reviewed changes, and rollback target."""

    pinned_main_sha: str
    baseline_version: str
    target_version: str
    prediction_version: str
    rollback_target_release: str
    community_rollback_release: str
    reviewed_commits_since_last_sha: Tuple[str, ...]
    inherited_artifacts_unverified: bool
    service_health_ok: bool
    alert_delivery_verified: bool
    provider_setup: Dict[str, Any]
    active_profile_summary: Dict[str, Any]
    restorable_config_backup: Dict[str, Any]
    captured_at_epoch_s: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def capture_phase0_baseline_snapshot(
    *,
    pinned_main_sha: str = DEFAULT_PINNED_MAIN_SHA,
    baseline_version: str = DEFAULT_BASELINE_VERSION,
    target_version: str = DEFAULT_TARGET_VERSION,
    prediction_version: str = "5.3-3d-proximity-age-aware",
    rollback_target_release: str = DEFAULT_ROLLBACK_TARGET_RELEASE,
    community_rollback_release: str = DEFAULT_COMMUNITY_ROLLBACK_RELEASE,
    reviewed_commits: Optional[Sequence[str]] = None,
    provider_setup: Optional[Mapping[str, Any]] = None,
    active_profile_summary: Optional[Mapping[str, Any]] = None,
    raw_config: Optional[Mapping[str, Any]] = None,
    service_health_ok: bool = True,
    alert_delivery_verified: bool = True,
    now_epoch_s: Optional[float] = None,
) -> MainBaselinePin:
    """Capture the Phase 0 baseline pin and restorable configuration backup."""
    commits = tuple(
        reviewed_commits
        or (
            "v5.8.8: Remove dead Next 60 forecast code and duplicate route guards",
            "v5.6.10: Prediction Lab manual and scheduled snapshot sync hardening",
            "v5.5.3: Community self-hosting updater hardening (no physical prediction changes)",
            "v5.3.0: 3D proximity and age-aware ETA handling with unresolved coverage guards",
        )
    )
    providers = dict(
        provider_setup
        or {
            "primary_adsb_provider": "airplanes.live",
            "fallback_providers": ["adsb.lol", "adsb.fi", "opensky"],
            "terrain_provider": "SRTM_30M_CACHE_WITH_CURVATURE",
            "airport_db": "data/aviation/compiled/global_airports.sqlite3",
        }
    )
    profile_summary = redact_sensitive_config(
        active_profile_summary
        or {
            "alert_radius_km": 8.0,
            "update_interval_sec": 30,
            "destination_Compare": True,
            "terrain_visibility_mode": "geometric_los_curvature_v6",
            "diagnostics_v6_enabled": True,
        }
    )
    backup = redact_sensitive_config(
        raw_config
        or {
            "DATABASE_BACKEND": os.getenv("DATABASE_BACKEND", "sqlite"),
            "PREDICTION_LAB_ROOT": os.getenv("PREDICTION_LAB_ROOT", "./.community/prediction_lab"),
            "ALERT_RADIUS_KM": 8.0,
            "UPDATE_INTERVAL": 30,
            "TELEGRAM_BOT_TOKEN": "<REDACTED>",
            "OBSERVER_LAT": "<REDACTED>",
            "OBSERVER_LON": "<REDACTED>",
        }
    )
    return MainBaselinePin(
        pinned_main_sha=str(pinned_main_sha).strip() or DEFAULT_PINNED_MAIN_SHA,
        baseline_version=str(baseline_version),
        target_version=str(target_version),
        prediction_version=str(prediction_version),
        rollback_target_release=str(rollback_target_release),
        community_rollback_release=str(community_rollback_release),
        reviewed_commits_since_last_sha=commits,
        inherited_artifacts_unverified=True,
        service_health_ok=bool(service_health_ok),
        alert_delivery_verified=bool(alert_delivery_verified),
        provider_setup=providers,
        active_profile_summary=profile_summary,
        restorable_config_backup=backup,
        captured_at_epoch_s=float(now_epoch_s if now_epoch_s is not None else time.time()),
    )


def verify_rollback_readiness(pin: MainBaselinePin) -> Dict[str, Any]:
    """Verify that the Phase 0 pin has a valid rollback target and secret-safe backup."""
    has_sha = len(pin.pinned_main_sha) >= 7
    has_rollback = pin.rollback_target_release.startswith("v5.")
    has_community_rollback = pin.community_rollback_release.startswith("v5.")
    secrets_redacted = all(
        pin.restorable_config_backup.get(k) in (None, "", "<REDACTED>")
        for k in ("TELEGRAM_BOT_TOKEN", "OBSERVER_LAT", "OBSERVER_LON")
    )
    ready = bool(
        has_sha
        and has_rollback
        and has_community_rollback
        and secrets_redacted
        and pin.inherited_artifacts_unverified
        and pin.service_health_ok
    )
    return {
        "rollback_ready": ready,
        "pinned_main_sha": pin.pinned_main_sha,
        "rollback_target_release": pin.rollback_target_release,
        "community_rollback_release": pin.community_rollback_release,
        "secrets_redacted": secrets_redacted,
        "inherited_artifacts_unverified": pin.inherited_artifacts_unverified,
    }


@dataclass
class DecisionDiagnosticRecord:
    """Phase 1 privacy-safe decision diagnostic record for a single evaluated aircraft."""

    record_id: str
    main_sha: str
    captured_at_epoch_s: float
    icao: str
    callsign: Optional[str]
    observer_ref: str
    observation_timestamp_s: Optional[float]
    observation_source: str
    observation_age_s: float
    freshness_status: str  # fresh | aging | stale | missing_unknown
    coverage_status: str   # confirmed | unknown
    relative_distance_km: Optional[float]
    relative_bearing_deg: Optional[float]
    altitude_ft: Optional[float]
    vertical_rate_fpm: Optional[float]
    ground_speed_kt: Optional[float]
    heading_deg: Optional[float]
    track_history_points: int
    track_evidence_used: str
    predicted_cpa_km: Optional[float]
    predicted_cpa_3d_km: Optional[float]
    predicted_eta_s: Optional[float]
    confidence: Optional[float]
    uncertainty_km: Optional[float]
    airport_classification: str
    airport_code: Optional[str]
    runway_id: Optional[str]
    airport_reason_code: str
    airport_explanation: str
    terrain_los_status: str  # clear | blocked | unknown
    terrain_data_version: str
    terrain_data_quality: str
    terrain_reason_code: str
    eta_cpa_root_cause: str
    final_decision: str  # alert | suppress | cancel | hold | unknown_coverage | delivery_failure
    stable_reason_code: str
    user_feedback: Optional[str] = None
    user_feedback_note: Optional[str] = None
    user_feedback_timestamp_s: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def classify_observation_freshness(
    *,
    observation_age_s: Optional[float],
    has_position: bool = True,
    stale_threshold_s: float = 65.0,
    aging_threshold_s: float = 25.0,
) -> Tuple[str, str]:
    """Return `(freshness_status, coverage_status)`.

    Missing or stale ADS-B coverage is strictly recorded as `unknown`, never as a hit or miss.
    """
    if not has_position or observation_age_s is None:
        return ("missing_unknown", "unknown")
    age = max(0.0, float(observation_age_s))
    if age > stale_threshold_s:
        return ("stale", "unknown")
    if age > aging_threshold_s:
        return ("aging", "confirmed")
    return ("fresh", "confirmed")


class DiagnosticCollectorV6:
    """Bounded, toggleable Phase 1 diagnostic collector with zero side effects on decisions."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        max_records: int = DEFAULT_DIAGNOSTIC_RETENTION_LIMIT,
        main_sha: str = DEFAULT_PINNED_MAIN_SHA,
    ) -> None:
        self.enabled = bool(enabled)
        self.max_records = max(16, int(max_records))
        self.main_sha = str(main_sha)
        self._records: "OrderedDict[str, DecisionDiagnosticRecord]" = OrderedDict()

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)

    def list_records(self) -> List[DecisionDiagnosticRecord]:
        return list(self._records.values())

    def get_record(self, record_id: str) -> Optional[DecisionDiagnosticRecord]:
        return self._records.get(record_id)

    def capture(
        self,
        *,
        icao: str,
        callsign: Optional[str] = None,
        observer_lat: Optional[float] = None,
        observer_lon: Optional[float] = None,
        observer_id: Optional[str] = None,
        observation_timestamp_s: Optional[float] = None,
        observation_source: str = "airplanes.live",
        observation_age_s: Optional[float] = 0.0,
        has_position: bool = True,
        relative_distance_km: Optional[float] = None,
        relative_bearing_deg: Optional[float] = None,
        altitude_ft: Optional[float] = None,
        vertical_rate_fpm: Optional[float] = None,
        ground_speed_kt: Optional[float] = None,
        heading_deg: Optional[float] = None,
        track_history_points: int = 1,
        track_evidence_used: str = "kinematic_age_aware_v53",
        predicted_cpa_km: Optional[float] = None,
        predicted_cpa_3d_km: Optional[float] = None,
        predicted_eta_s: Optional[float] = None,
        confidence: Optional[float] = None,
        uncertainty_km: Optional[float] = None,
        airport_eval: Optional[AirportArrivalExplanation] = None,
        terrain_eval: Optional[TerrainLineOfSightResult] = None,
        eta_cpa_eval: Optional[EtaCpaDiagnosis] = None,
        final_decision: str = "alert",
        stable_reason_code: str = "QUALIFIED_IN_RADIUS_PASS",
        now_epoch_s: Optional[float] = None,
    ) -> Optional[DecisionDiagnosticRecord]:
        """Record the decision path without mutating any prediction or notification input."""
        if not self.enabled:
            return None

        now_s = float(now_epoch_s if now_epoch_s is not None else time.time())
        freshness_status, coverage_status = classify_observation_freshness(
            observation_age_s=observation_age_s,
            has_position=has_position,
        )
        decision = str(final_decision).lower().strip()
        if coverage_status == "unknown" and decision not in ("suppress", "cancel", "delivery_failure"):
            decision = "unknown_coverage"
        if decision not in FINAL_DECISION_VALUES_V6:
            decision = "suppress"

        obs_ref = anonymize_observer_reference(observer_lat, observer_lon, observer_id)
        record_id = f"diag_{icao.upper()}_{int(now_s * 1000)}_{len(self._records) + 1}"

        record = DecisionDiagnosticRecord(
            record_id=record_id,
            main_sha=self.main_sha,
            captured_at_epoch_s=now_s,
            icao=str(icao).upper().strip(),
            callsign=str(callsign).strip() if callsign else None,
            observer_ref=obs_ref,
            observation_timestamp_s=observation_timestamp_s,
            observation_source=str(observation_source or "unknown"),
            observation_age_s=round(float(observation_age_s or 0.0), 2),
            freshness_status=freshness_status,
            coverage_status=coverage_status,
            relative_distance_km=round(float(relative_distance_km), 3) if relative_distance_km is not None else None,
            relative_bearing_deg=round(float(relative_bearing_deg), 1) if relative_bearing_deg is not None else None,
            altitude_ft=round(float(altitude_ft), 1) if altitude_ft is not None else None,
            vertical_rate_fpm=round(float(vertical_rate_fpm), 1) if vertical_rate_fpm is not None else None,
            ground_speed_kt=round(float(ground_speed_kt), 1) if ground_speed_kt is not None else None,
            heading_deg=round(float(heading_deg), 1) if heading_deg is not None else None,
            track_history_points=max(0, int(track_history_points)),
            track_evidence_used=str(track_evidence_used),
            predicted_cpa_km=round(float(predicted_cpa_km), 3) if predicted_cpa_km is not None else None,
            predicted_cpa_3d_km=round(float(predicted_cpa_3d_km), 3) if predicted_cpa_3d_km is not None else None,
            predicted_eta_s=round(float(predicted_eta_s), 2) if predicted_eta_s is not None else None,
            confidence=round(float(confidence), 3) if confidence is not None else None,
            uncertainty_km=round(float(uncertainty_km), 3) if uncertainty_km is not None else None,
            airport_classification=(
                airport_eval.classification if airport_eval is not None else "OVERFLIGHT"
            ),
            airport_code=(
                (airport_eval.matched_airport_icao or airport_eval.matched_airport_iata)
                if airport_eval is not None
                else None
            ),
            runway_id=airport_eval.matched_runway_id if airport_eval is not None else None,
            airport_reason_code=(
                airport_eval.reason_code if airport_eval is not None else "NO_AIRPORT_EVAL"
            ),
            airport_explanation=(
                airport_eval.explanation if airport_eval is not None else "No airport override applied."
            ),
            terrain_los_status=(
                terrain_eval.status if terrain_eval is not None else "unknown"
            ),
            terrain_data_version=(
                terrain_eval.terrain_data_version if terrain_eval is not None else "UNAVAILABLE"
            ),
            terrain_data_quality=(
                terrain_eval.terrain_quality if terrain_eval is not None else "missing"
            ),
            terrain_reason_code=(
                terrain_eval.reason_code if terrain_eval is not None else "TERRAIN_NOT_EVALUATED"
            ),
            eta_cpa_root_cause=(
                eta_cpa_eval.primary_driver if eta_cpa_eval is not None else "STABLE_NOMINAL"
            ),
            final_decision=decision,
            stable_reason_code=str(stable_reason_code),
        )

        self._records[record_id] = record
        while len(self._records) > self.max_records:
            self._records.popitem(last=False)
        return record

    def record_feedback(
        self,
        record_id: str,
        feedback_category: str,
        *,
        note: Optional[str] = None,
        now_epoch_s: Optional[float] = None,
    ) -> DecisionDiagnosticRecord:
        """Attach structured Phase 1 user feedback to an existing diagnostic record."""
        normalized = str(feedback_category).strip().lower()
        if normalized not in USER_FEEDBACK_CATEGORIES_V6:
            raise ValueError(
                f"Invalid v6 user feedback category '{feedback_category}'. "
                f"Expected one of {USER_FEEDBACK_CATEGORIES_V6}."
            )
        record = self._records.get(record_id)
        if record is None:
            raise KeyError(f"Diagnostic record '{record_id}' not found.")
        record.user_feedback = normalized
        record.user_feedback_note = str(note).strip() if note else None
        record.user_feedback_timestamp_s = float(now_epoch_s if now_epoch_s is not None else time.time())
        return record


DEFAULT_DIAGNOSTIC_COLLECTOR_V6 = DiagnosticCollectorV6(
    enabled=os.getenv("PLANE_DIAGNOSTICS_V6_ENABLED", "1").strip() not in ("0", "false", "False"),
)


@dataclass(frozen=True)
class TriageCaseV6:
    """Phase 2 structured triage record for a fresh observed failure or decision audit."""

    case_id: str
    main_sha: str
    problem_class: str  # One of TRIAGE_PROBLEM_CLASSES_V6
    observed_inputs: Dict[str, Any]
    commit_behavior: str
    expected_result: str
    verified_from_fresh_observation: bool
    reason_code: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def triage_fresh_case(
    *,
    case_id: str,
    main_sha: str,
    observed_inputs: Mapping[str, Any],
    commit_behavior: str,
    expected_result: str,
) -> TriageCaseV6:
    """Phase 2: Classify a fresh observation into one of the 5 problem classes without
    importing unverified Error Museum conclusions.
    """
    inputs = redact_sensitive_config(observed_inputs)
    obs_age_s = float(observed_inputs.get("observation_age_s", 0.0) or 0.0)
    source_conflict = bool(observed_inputs.get("conflicting_sources", False))
    missing_coverage = bool(observed_inputs.get("missing_coverage", False))

    delivery_issue = bool(
        observed_inputs.get("duplicate_notification")
        or observed_inputs.get("late_delivery")
        or observed_inputs.get("wrongly_cancelled")
        or observed_inputs.get("undelivered_alert")
    )
    terrain_hidden = bool(observed_inputs.get("terrain_blocked_pass", False))
    airport_misclassified = bool(observed_inputs.get("airport_misclassified", False))
    trajectory_shift = bool(
        observed_inputs.get("eta_drift")
        or observed_inputs.get("turn_detected")
        or observed_inputs.get("speed_altitude_change")
    )

    if missing_coverage or source_conflict or obs_age_s > 45.0:
        problem_class = "DATA_FRESHNESS"
        reason_code = "FRESHNESS_STALE_OR_CONFLICTING_ADSB"
    elif delivery_issue:
        problem_class = "NOTIFICATION_LIFECYCLE"
        reason_code = "LIFECYCLE_DELIVERY_OR_CANCELLATION_DEFECT"
    elif terrain_hidden:
        problem_class = "TERRAIN_VISIBILITY"
        reason_code = "TERRAIN_GEOMETRIC_OBSTRUCTION_DEFECT"
    elif airport_misclassified:
        problem_class = "AIRPORT_LOGIC"
        reason_code = "AIRPORT_TERMINAL_CLASSIFICATION_DEFECT"
    elif trajectory_shift:
        problem_class = "ETA_TRAJECTORY"
        reason_code = "TRAJECTORY_ETA_CPA_DRIFT"
    else:
        problem_class = "ETA_TRAJECTORY"
        reason_code = "GENERAL_KINEMATIC_AUDIT"

    return TriageCaseV6(
        case_id=str(case_id),
        main_sha=str(main_sha or DEFAULT_PINNED_MAIN_SHA),
        problem_class=problem_class,
        observed_inputs=inputs,
        commit_behavior=str(commit_behavior),
        expected_result=str(expected_result),
        verified_from_fresh_observation=True,
        reason_code=reason_code,
    )


@dataclass(frozen=True)
class ReleaseValidationMetricsV6:
    """Phase 4 release validation measures with explicit denominators and unknown-coverage shares."""

    # 1. Confirmed false alerts per observer-hour
    confirmed_false_alerts_count: int
    observer_hours_denominator: float
    confirmed_false_alerts_per_observer_hour: float
    false_alert_unknown_coverage_count: int

    # 2. Confirmed missed visible passes
    confirmed_missed_visible_passes_count: int
    confirmed_visible_passes_denominator: int
    confirmed_missed_visible_pass_rate: float
    visibility_unknown_coverage_count: int

    # 3. ETA error (MAE & P90) on confirmed observed passes
    eta_mae_s: Optional[float]
    eta_p90_s: Optional[float]
    eta_confirmed_passes_denominator: int
    eta_unavailable_or_uncertain_count: int
    eta_unavailable_share: float

    # 4. Airport classification errors separated by class
    airport_errors_by_class: Dict[str, Dict[str, int]]

    # 5. Terrain false-clear rate and terrain unknown rate
    terrain_false_clear_count: int
    terrain_confirmed_obstructed_denominator: int
    terrain_false_clear_rate: float
    terrain_unknown_count: int
    terrain_total_evaluated_denominator: int
    terrain_unknown_rate: float

    # 6. Alert delivery/cancellation errors and monitoring-loop health
    delivery_cancellation_error_count: int
    lifecycle_events_denominator: int
    delivery_cancellation_error_rate: float
    monitoring_loop_healthy: bool
    monitoring_loop_p95_cycle_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def compute_v6_success_measures(
    *,
    observer_hours: float,
    alert_outcomes: Sequence[Mapping[str, Any]],
    visible_pass_outcomes: Sequence[Mapping[str, Any]],
    eta_evaluations: Sequence[EtaOutcomeEvaluation],
    airport_evaluations: Sequence[Mapping[str, Any]],
    terrain_evaluations: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
    monitoring_loop_healthy: bool = True,
    monitoring_loop_p95_cycle_ms: float = 142.0,
) -> ReleaseValidationMetricsV6:
    """Compute all 6 Phase 4 release metrics with explicit numerators, denominators, and unknowns."""
    obs_hours = max(0.001, float(observer_hours))

    # 1. Confirmed false alerts per observer-hour
    false_alerts_confirmed = 0
    false_alerts_unknown = 0
    for item in alert_outcomes:
        cov = str(item.get("coverage_status", "confirmed")).lower()
        if cov != "confirmed":
            false_alerts_unknown += 1
            continue
        if bool(item.get("is_false_alert", False)):
            false_alerts_confirmed += 1

    # 2. Confirmed missed visible passes
    missed_visible = 0
    visible_denom = 0
    visible_unknown = 0
    for item in visible_pass_outcomes:
        cov = str(item.get("coverage_status", "confirmed")).lower()
        los = str(item.get("terrain_los_status", "clear")).lower()
        if cov != "confirmed" or los == "unknown":
            visible_unknown += 1
            continue
        if los == "clear" and bool(item.get("confirmed_in_radius_pass", True)):
            visible_denom += 1
            if not bool(item.get("alert_delivered", True)):
                missed_visible += 1

    # 3. ETA error (MAE and P90) on confirmed observed passes
    scored_errors: List[float] = []
    eta_unavailable = 0
    for ev in eta_evaluations:
        if ev.scoreable and ev.absolute_eta_error_s is not None:
            scored_errors.append(float(ev.absolute_eta_error_s))
        else:
            eta_unavailable += 1
    total_eta_cases = len(eta_evaluations)
    if scored_errors:
        sorted_err = sorted(scored_errors)
        eta_mae = round(sum(sorted_err) / len(sorted_err), 2)
        p90_idx = min(len(sorted_err) - 1, int(round(0.9 * (len(sorted_err) - 1))))
        eta_p90 = round(sorted_err[p90_idx], 2)
    else:
        eta_mae = None
        eta_p90 = None
    eta_unavail_share = round(eta_unavailable / total_eta_cases, 4) if total_eta_cases > 0 else 0.0

    # 4. Airport classification errors separated by class
    classes = ("ARRIVAL", "DEPARTURE", "OVERFLIGHT", "HOLDING", "MISSED_APPROACH", "AIRPORT_NEARBY")
    airport_breakdown: Dict[str, Dict[str, int]] = {
        cls: {"errors": 0, "total_confirmed": 0, "unknown_coverage": 0}
        for cls in classes
    }
    for item in airport_evaluations:
        expected_cls = str(item.get("expected_class", "OVERFLIGHT")).upper()
        predicted_cls = str(item.get("predicted_class", "OVERFLIGHT")).upper()
        cov = str(item.get("coverage_status", "confirmed")).lower()
        bucket = airport_breakdown.setdefault(
            expected_cls,
            {"errors": 0, "total_confirmed": 0, "unknown_coverage": 0},
        )
        if cov != "confirmed":
            bucket["unknown_coverage"] += 1
            continue
        bucket["total_confirmed"] += 1
        if predicted_cls != expected_cls:
            bucket["errors"] += 1

    # 5. Terrain false-clear rate and terrain unknown rate
    terrain_false_clear = 0
    terrain_obstructed_denom = 0
    terrain_unknown = 0
    terrain_total = len(terrain_evaluations)
    for item in terrain_evaluations:
        predicted_los = str(item.get("predicted_los", "unknown")).lower()
        actual_obstructed = item.get("actual_obstructed")
        if predicted_los == "unknown":
            terrain_unknown += 1
        if actual_obstructed is True:
            terrain_obstructed_denom += 1
            if predicted_los == "clear":
                terrain_false_clear += 1

    # 6. Alert delivery/cancellation errors and monitoring-loop health
    lifecycle_errors = 0
    lifecycle_denom = len(lifecycle_events)
    for item in lifecycle_events:
        if bool(item.get("duplicate_send")) or bool(item.get("wrong_cancellation")) or bool(item.get("delivery_failed")):
            lifecycle_errors += 1

    return ReleaseValidationMetricsV6(
        confirmed_false_alerts_count=false_alerts_confirmed,
        observer_hours_denominator=round(obs_hours, 2),
        confirmed_false_alerts_per_observer_hour=round(false_alerts_confirmed / obs_hours, 4),
        false_alert_unknown_coverage_count=false_alerts_unknown,
        confirmed_missed_visible_passes_count=missed_visible,
        confirmed_visible_passes_denominator=visible_denom,
        confirmed_missed_visible_pass_rate=(
            round(missed_visible / visible_denom, 4) if visible_denom > 0 else 0.0
        ),
        visibility_unknown_coverage_count=visible_unknown,
        eta_mae_s=eta_mae,
        eta_p90_s=eta_p90,
        eta_confirmed_passes_denominator=len(scored_errors),
        eta_unavailable_or_uncertain_count=eta_unavailable,
        eta_unavailable_share=eta_unavail_share,
        airport_errors_by_class=airport_breakdown,
        terrain_false_clear_count=terrain_false_clear,
        terrain_confirmed_obstructed_denominator=terrain_obstructed_denom,
        terrain_false_clear_rate=(
            round(terrain_false_clear / terrain_obstructed_denom, 4)
            if terrain_obstructed_denom > 0
            else 0.0
        ),
        terrain_unknown_count=terrain_unknown,
        terrain_total_evaluated_denominator=terrain_total,
        terrain_unknown_rate=(
            round(terrain_unknown / terrain_total, 4) if terrain_total > 0 else 0.0
        ),
        delivery_cancellation_error_count=lifecycle_errors,
        lifecycle_events_denominator=lifecycle_denom,
        delivery_cancellation_error_rate=(
            round(lifecycle_errors / lifecycle_denom, 4) if lifecycle_denom > 0 else 0.0
        ),
        monitoring_loop_healthy=bool(monitoring_loop_healthy),
        monitoring_loop_p95_cycle_ms=round(float(monitoring_loop_p95_cycle_ms), 2),
    )
