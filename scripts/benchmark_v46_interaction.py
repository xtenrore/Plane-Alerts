"""Synthetic handler-side Telegram callback latency benchmark for v4.6.

Telegram/network transport latency is intentionally excluded. This measures the
avoidable Plane Alerts delay between callback receipt and answerCallbackQuery.
"""
from __future__ import annotations

import asyncio
import json
import time
from statistics import median
from types import SimpleNamespace

from telegram.ext import ApplicationHandlerStop

from app.bot import interaction_v46 as interaction
from app.bot import next60

DB_DELAY_S = 0.04
ITERATIONS = 20


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _stats(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
        "max_ms": round(max(values), 3),
    }


class FakeMessage:
    async def reply_text(self, *args, **kwargs):
        return None


class FakeQuery:
    def __init__(self, query_id: str, ack_times: list[float], started: float) -> None:
        self.id = query_id
        self.data = f"{next60.MORE_PREFIX}THY123"
        self.message = FakeMessage()
        self._ack_times = ack_times
        self._started = started

    async def answer(self, *args, **kwargs):
        self._ack_times.append((time.perf_counter() - self._started) * 1000.0)
        return True


async def _slow_docs(user_id, now):
    del user_id, now
    await asyncio.sleep(DB_DELAY_S)
    return []


async def _legacy_order(update) -> None:
    """Model the verified v4.5 /next60 ordering: DB first, callback ACK second."""
    await _slow_docs(update.effective_user.id, None)
    await update.callback_query.answer()


async def _run() -> tuple[list[float], list[float]]:
    original = next60.build_next60_docs
    next60.build_next60_docs = _slow_docs
    before: list[float] = []
    after: list[float] = []
    try:
        for index in range(ITERATIONS):
            started = time.perf_counter()
            query = FakeQuery(f"legacy-{index}", before, started)
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))
            await _legacy_order(update)

        interaction.clear_latency_state_for_tests()
        for index in range(ITERATIONS):
            started = time.perf_counter()
            query = FakeQuery(f"v46-{index}", after, started)
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))
            try:
                await interaction.next60_more_v46(update, None)
            except ApplicationHandlerStop:
                pass
    finally:
        next60.build_next60_docs = original
        interaction.clear_latency_state_for_tests()
    return before, after


def main() -> None:
    before, after = asyncio.run(_run())
    before_stats = _stats(before)
    after_stats = _stats(after)
    if before_stats["p95_ms"] < DB_DELAY_S * 1000.0 * 0.80:
        raise SystemExit(f"legacy benchmark did not include the synthetic DB delay: {before_stats}")
    if after_stats["p95_ms"] > 8.0:
        raise SystemExit(f"v4.6 handler adds too much pre-ACK delay: {after_stats}")
    if after_stats["p95_ms"] >= before_stats["p95_ms"] * 0.25:
        raise SystemExit(f"v4.6 pre-ACK latency did not improve enough: before={before_stats} after={after_stats}")
    print("V46_INTERACTION_BENCHMARK_JSON=" + json.dumps({
        "scope": "handler_side_only_excludes_telegram_network",
        "synthetic_db_delay_ms": DB_DELAY_S * 1000.0,
        "before_ack": before_stats,
        "after_ack": after_stats,
        "iterations": ITERATIONS,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
