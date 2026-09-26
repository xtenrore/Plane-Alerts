"""Inline keyboard additions for v3.2 photography actions."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.flight_links import flightradar24_url
from app.bot.keyboards import CB_FB_DISLIKE_PREFIX, CB_FB_LIKE_PREFIX

PHOTO_CALLBACK_PREFIX = "photo:"


def notification_actions_keyboard(
    notification_id: str,
    icao24: str | None = None,
    callsign: str | None = None,
) -> InlineKeyboardMarkup:
    """Aircraft alert actions: Flightradar24, camera settings, and feedback."""
    rows: list[list[InlineKeyboardButton]] = []
    tracker_url = flightradar24_url(callsign=callsign, icao24=icao24)
    if tracker_url:
        rows.append([
            InlineKeyboardButton(
                "Open in Flightradar24",
                url=tracker_url,
            )
        ])
    rows.extend([
        [InlineKeyboardButton("📷 Best camera settings", callback_data=f"{PHOTO_CALLBACK_PREFIX}{notification_id}")],
        [
            InlineKeyboardButton("👍 Helpful", callback_data=f"{CB_FB_LIKE_PREFIX}{notification_id}"),
            InlineKeyboardButton("👎 Not Helpful / Wrong", callback_data=f"{CB_FB_DISLIKE_PREFIX}{notification_id}"),
        ],
    ])
    return InlineKeyboardMarkup(rows)
