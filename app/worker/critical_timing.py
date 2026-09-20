"""Critical live-alert timing guards."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Iterable

from app.aircraft.providers import ProviderManager
from app.intelligence import trajectory as trajectory_mod

logger = logging.getLogger(__name__)
_PROVIDER_DEADLINE_S = 1.8
_LIVE_ALERT_MAX_POSITION_AGE_S = 12.0
_INSTALLED = False
_ORIGINAL_SAFE_QUERY = ProviderManager._safe_query
_ORIGINAL_PREDICT_TRAJECTORY = trajectory_mod.predict_trajectory


def _effective_latest_age(samples: Iterable[Any], *, now: float | None = None) -> float | None:
    values = list(samples)
    if not values:
        return None
    latest = max(values, key=lambda item: float(getattr(item, "timestamp", 0.0) or 0.0))
    now = time.time() if now is None else float(now)
    try:
        timestamp = float(getattr(latest, "timestamp", now) or now)
        provider_age = float(getattr(latest, "position_age_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    return max(0.0, provider_age, now - timestamp)


def _harden_prediction_freshness(prediction: Any, samples: Iterable[Any], *, now: float | None = None):
    age = _effective_latest_age(samples, now=now)
    if age is None or age <= _LIVE_ALERT_MAX_POSITION_AGE_S:
        return prediction
    return replace(prediction, state="Prediction uncertain", confidence="Uncertain", confidence_score=min(0.25, float(getattr(prediction, "confidence_score", 0.0) or 0.0)), enters_alert_radius=False, stale=True, reason=f"ADS-B position age {age:.1f}s exceeds the {_LIVE_ALERT_MAX_POSITION_AGE_S:.0f}s live-alert freshness limit")


async def _bounded_safe_query(self: ProviderManager, provider: Any, latitude: float, longitude: float, radius_nm: int):
    try:
        return await asyncio.wait_for(_ORIGINAL_SAFE_QUERY(self, provider, latitude, longitude, radius_nm), timeout=_PROVIDER_DEADLINE_S)
    except asyncio.TimeoutError:
        if hasattr(provider, "record_failure"):
            provider.record_failure(f"Critical-path timeout (>{_PROVIDER_DEADLINE_S:.1f}s)", timeout=True)
        else:
            provider.error_count = int(getattr(provider, "error_count", 0) or 0) + 1
            provider.last_error = f"Critical-path timeout (>{_PROVIDER_DEADLINE_S:.1f}s)"
        logger.warning("adsb_provider_critical_timeout provider=%s deadline_s=%.1f circuit=%s cooldown_s=%.1f", getattr(provider, "name", "unknown"), _PROVIDER_DEADLINE_S, getattr(provider, "circuit_state", "unknown"), float(getattr(provider, "cooldown_remaining_s", 0.0) or 0.0))
        return []


def _critical_predict_trajectory(samples, user_lat, user_lon, alert_radius_km, **kwargs):
    sample_list = list(samples)
    prediction = _ORIGINAL_PREDICT_TRAJECTORY(sample_list, user_lat, user_lon, alert_radius_km, **kwargs)
    return _harden_prediction_freshness(prediction, sample_list, now=kwargs.get("now"))


def install_critical_timing_guards() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    ProviderManager._safe_query = _bounded_safe_query
    trajectory_mod.predict_trajectory = _critical_predict_trajectory
    _INSTALLED = True
    logger.info("Critical alert timing guards enabled: provider_deadline=%.1fs live_position_max_age=%.0fs", _PROVIDER_DEADLINE_S, _LIVE_ALERT_MAX_POSITION_AGE_S)
