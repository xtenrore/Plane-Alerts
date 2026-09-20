"""User-safe Telegram handling for temporary v4.8 storage failures."""
from __future__ import annotations

import asyncio
import logging

from pymongo.errors import PyMongoError
from telegram import Update
from telegram.ext import Application, ContextTypes

logger = logging.getLogger(__name__)

_STORAGE_MESSAGE = "Settings are temporarily unavailable. Live aircraft monitoring is still running."


def _is_storage_error(exc: BaseException | None) -> bool:
    if isinstance(exc, PyMongoError):
        return True
    if isinstance(exc, RuntimeError) and "database not initialised" in str(exc).lower():
        return True
    return False


async def _storage_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if not _is_storage_error(exc):
        logger.error("telegram_handler_failed error=%s", type(exc).__name__ if exc else "unknown")
        return

    logger.warning("telegram_storage_temporarily_unavailable error=%s", type(exc).__name__)
    if not isinstance(update, Update):
        return
    query = update.callback_query
    try:
        if query:
            try:
                await query.answer("Settings are temporarily unavailable.", show_alert=True)
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(_STORAGE_MESSAGE)
                return
        if update.effective_message:
            await update.effective_message.reply_text(_STORAGE_MESSAGE)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The error path must never cascade into another user-visible traceback.
        logger.warning("telegram_storage_error_notice_failed")


def register_storage_failure_handler(application: Application) -> None:
    application.add_error_handler(_storage_error_handler)
