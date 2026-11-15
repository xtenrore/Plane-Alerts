"""Bounded observability for the user-invoked Next60 forecast path.

The command previously returned an empty forecast without leaving enough runtime
evidence to distinguish a legitimate empty hour from a live-state query problem
or a volume-history reader problem.  These wrappers do not alter candidate
selection, CPA/ETA math, route authority, history forecasting, or Telegram text.
They only log counts, latency, and safe storage metadata when Next60 is actually
built by a user request.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.operational_volume_v561 import route_db_path

logger = logging.getLogger(__name__)

_INSTALLED = False


def _volume_snapshot() -> tuple[bool, int]:
    try:
        path: Path = route_db_path()
        return path.exists(), path.stat().st_size if path.exists() else 0
    except OSError:
        return False, 0


def install_next60_diagnostics() -> bool:
    """Instrument Next60 source counts without changing any forecast semantics."""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        from app.bot import next60
    except Exception:
        logger.exception("next60_diagnostics_install_failed reason=import")
        return False

    if getattr(next60, "_next60_diagnostics_installed", False):
        _INSTALLED = True
        return True

    history_reader: Callable[[int, Any], Awaitable[list[dict[str, Any]]]] = next60._history_docs
    live_reader: Callable[[int, Any], Awaitable[list[dict[str, Any]]]] = next60._live_docs
    build_reader: Callable[[int, Any], Awaitable[list[dict[str, Any]]]] = next60.build_next60_docs

    async def history_with_diagnostics(user_id: int, now: Any) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            docs = await history_reader(user_id, now)
        except Exception:
            exists, size = _volume_snapshot()
            logger.exception(
                "next60_history_failed user=%s volume_exists=%s volume_bytes=%d",
                int(user_id),
                exists,
                size,
            )
            raise
        exists, size = _volume_snapshot()
        logger.info(
            "next60_history user=%s docs=%d source=%s volume_exists=%s volume_bytes=%d duration_ms=%.1f",
            int(user_id),
            len(docs),
            "volume" if getattr(next60, "_volume_history_recovery_v589", False) else "legacy",
            exists,
            size,
            (time.perf_counter() - started) * 1000.0,
        )
        return docs

    async def live_with_diagnostics(user_id: int, now: Any) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            docs = await live_reader(user_id, now)
        except Exception:
            logger.exception("next60_live_failed user=%s", int(user_id))
            raise
        logger.info(
            "next60_live user=%s docs=%d duration_ms=%.1f",
            int(user_id),
            len(docs),
            (time.perf_counter() - started) * 1000.0,
        )
        return docs

    async def build_with_diagnostics(user_id: int, now: Any) -> list[dict[str, Any]]:
        started = time.perf_counter()
        try:
            docs = await build_reader(user_id, now)
        except Exception:
            logger.exception("next60_build_failed user=%s", int(user_id))
            raise
        live_count = sum(1 for doc in docs if str(doc.get("source") or "") == "live")
        history_count = sum(1 for doc in docs if str(doc.get("source") or "") == "history")
        logger.info(
            "next60_build user=%s merged=%d live=%d history=%d empty=%s duration_ms=%.1f",
            int(user_id),
            len(docs),
            live_count,
            history_count,
            not bool(docs),
            (time.perf_counter() - started) * 1000.0,
        )
        return docs

    # build_next60_docs resolves _history_docs/_live_docs from module globals at
    # call time, so the original build function naturally calls the two wrappers.
    next60._history_docs = history_with_diagnostics
    next60._live_docs = live_with_diagnostics
    next60.build_next60_docs = build_with_diagnostics
    next60._next60_diagnostics_installed = True
    _INSTALLED = True
    logger.info("next60_diagnostics_enabled")
    return True
