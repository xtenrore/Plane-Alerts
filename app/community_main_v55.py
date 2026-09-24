"""AGY-free Plane Alerts runtime entry point for community self-hosting.

The owner's AGY tooling remains in the repository for private development, but
community services must never import, register, start, call or authenticate it.
This module stubs the sole runtime console import before loading app.main,
filters the private Telegram command, and applies community-only local receiver
coverage relevance using a receiver location that remains separate from user
Telegram profile locations.
"""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from telegram import Bot


# app.main imports this module by name. Install a zero-capability stub first so
# the real private AGY console is not part of the self-hosted runtime at all.
_stub = ModuleType("app.agy_console")
_stub.register_agy_console_handlers = lambda application: None  # type: ignore[attr-defined]
sys.modules["app.agy_console"] = _stub

from app.local_adsb_coverage_v55 import install_local_receiver_coverage_guard  # noqa: E402

install_local_receiver_coverage_guard()

_original_set_my_commands = Bot.set_my_commands


async def _community_set_my_commands(self: Bot, commands: Any, *args: Any, **kwargs: Any):
    filtered = [command for command in commands if getattr(command, "command", "") != "agy"]
    return await _original_set_my_commands(self, filtered, *args, **kwargs)


Bot.set_my_commands = _community_set_my_commands  # type: ignore[method-assign]

from app.main import app  # noqa: E402,F401  (import only after AGY isolation)

__all__ = ["app"]
