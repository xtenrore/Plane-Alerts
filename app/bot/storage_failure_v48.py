"""User-safe Telegram recovery and privacy-safe failure diagnostics."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from traceback import walk_tb

from pymongo.errors import PyMongoError
from telegram import Update
from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)

_STORAGE_MESSAGE = "Settings are temporarily unavailable. Live aircraft monitoring is still running."
_ACTION_MESSAGE = "That action couldn't finish. Please try again, or use /help to reopen a menu."


def _failure_frames(exc: BaseException | None) -> str:
    """Keep bounded code locations, never exception text, source or local data."""
    frames: deque[str] = deque(maxlen=8)
    if exc is not None:
        for frame, line in walk_tb(exc.__traceback__):
            module = str(frame.f_globals.get("__name__", ""))
            if module.startswith("app."):
                frames.append(f"{module}:{frame.f_code.co_name}:{line}")
    return ">".join(frames) or "unavailable"


def _is_storage_error(exc: BaseException | None) -> bool:
    if isinstance(exc, PyMongoError):
        return True
    if isinstance(exc, RuntimeError) and "database not initialised" in str(exc).lower():
        return True
    return False


async def _storage_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    if _is_storage_error(exc):
        logger.warning("telegram_storage_temporarily_unavailable error=%s", type(exc).__name__)
        message = _STORAGE_MESSAGE
        notice = "Settings are temporarily unavailable."
    else:
        logger.error(
            "telegram_handler_failed error=%s frames=%s",
            type(exc).__name__ if exc else "unknown", _failure_frames(exc),
        )
        message = _ACTION_MESSAGE
        notice = "That action couldn't finish. Please try again."
    if not isinstance(update, Update):
        return
    query = update.callback_query
    try:
        if query:
            try:
                await query.answer(notice, show_alert=True)
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message)
                return
        if update.effective_message:
            await update.effective_message.reply_text(message)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The error path must never cascade into another user-visible traceback.
        logger.warning("telegram_error_notice_failed")


def register_storage_failure_handler(application: Application) -> None:
    application.add_error_handler(_storage_error_handler)
