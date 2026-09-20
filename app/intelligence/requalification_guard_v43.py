"""Anti-oscillation guard for confirmed v4.2 alert cancellations.

Prediction Lab observed qualify/cancel/qualify oscillation. The monitor requires
three consecutive route-gate cancellation confirmations; mirror that boundary
here. Once an already-qualified encounter accumulates the same three suppress
cycles, predictive evidence alone may not immediately resurrect it. Fresh,
physical entry into the configured radius always releases the latch.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import math
import time
from typing import Any, Iterable

from app.intelligence import route_guard_v42 as v42
from app.intelligence import route_history as route_mod

CANCEL_LATCH_CONFIRMATIONS = 3
_LATCH_TTL_S = 1800.0
_MAX_STATES = 2048
_INSTALLED = False


@dataclass(slots=True)
class CancellationLatch:
    last_seen_mono: float
    suppress_count: int = 0
    latched: bool = False
    last_observation_at: float | None = None


_states: "OrderedDict[tuple, CancellationLatch]" = OrderedDict()


def _prune(now_mono: float) -> None:
    for key in list(_states):
        if now_mono - _states[key].last_seen_mono > _LATCH_TTL_S:
            _states.pop(key, None)
    while len(_states) > _MAX_STATES:
        _states.popitem(last=False)


def _fresh_inside(pred: Any, radius_km: float) -> bool:
    return (
        not bool(getattr(pred, "stale", False))
        and float(getattr(pred, "current_distance_km", math.inf))
        <= float(radius_km)
    )


def _apply_latch(
    result: route_mod.RouteGateResult,
    *,
    pred: Any,
    encounter_was_qualified: bool,
    state: CancellationLatch,
    radius_km: float,
    observed_at: float | None = None,
) -> route_mod.RouteGateResult:
    """Pure state transition used by runtime and regression tests."""
    if _fresh_inside(pred, radius_km):
        state.suppress_count = 0
        state.latched = False
        return result

    if state.latched:
        return replace(
            result,
            suppress_alert=True,
            reason="v4.3 CANCEL_LATCHED: predictive requalification blocked until fresh physical radius entry",
            qualification_state="CANCEL_LATCHED",
        )

    if observed_at is not None:
        if state.last_observation_at is not None and observed_at <= state.last_observation_at + 0.001:
            return result
        state.last_observation_at = observed_at
    if bool(getattr(pred, "stale", False)):
        return result
    if encounter_was_qualified and bool(result.suppress_alert):
        state.suppress_count += 1
        if state.suppress_count >= CANCEL_LATCH_CONFIRMATIONS:
            state.latched = True
            return replace(
                result,
                suppress_alert=True,
                reason="v4.3 CANCEL_LATCHED: confirmed cancellation protected from prediction jitter",
                qualification_state="CANCEL_LATCHED",
            )
    else:
        # A recovered qualifying cycle before cancellation confirmation resets
        # the same way the monitor resets its cancellation confirmation count.
        state.suppress_count = 0

    return result


async def evaluate_route_v43(
    self: route_mod.RouteHistoryService,
    ac: Any,
    pred: Any,
    *,
    user_lat: float,
    user_lon: float,
    alert_radius_km: float,
    current_samples: Iterable[Any],
) -> route_mod.RouteGateResult:
    current_samples = list(current_samples)
    result = await v42.evaluate_route_v42(
        self,
        ac,
        pred,
        user_lat=user_lat,
        user_lon=user_lon,
        alert_radius_km=alert_radius_km,
        current_samples=current_samples,
    )

    now_mono = time.monotonic()
    key = v42._encounter_key(ac, user_lat, user_lon, alert_radius_km)
    encounter = v42._encounters.get(key)
    encounter_was_qualified = bool(encounter and encounter.qualified)

    state = _states.get(key)
    if state is None:
        state = CancellationLatch(last_seen_mono=now_mono)
        _states[key] = state
    else:
        state.last_seen_mono = now_mono
        _states.move_to_end(key)

    result = _apply_latch(
        result,
        pred=pred,
        encounter_was_qualified=encounter_was_qualified,
        state=state,
        radius_km=alert_radius_km,
        observed_at=max((float(item.timestamp) for item in current_samples if hasattr(item, "timestamp")), default=None),
    )
    _prune(now_mono)
    return result


def reset_v43_requalification_state_for_tests() -> None:
    _states.clear()


def install_requalification_guard_v43() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    route_mod.RouteHistoryService.evaluate = evaluate_route_v43
    _INSTALLED = True
