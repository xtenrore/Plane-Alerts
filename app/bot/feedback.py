"""Handler for notification feedback (Like/Dislike).

When a user clicks 👍 or 👎:
- Persists feedback record in MongoDB.
- On 👍 (Like): Confirms positive feedback.
- On 👎 (Dislike):
  - Triggers incremental re-learning (+25 test planes) for the user's location.
  - Consults AI Judge to analyze why the notification was undesirable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode

from app.aircraft.ai_judge import ai_judge
from app.aircraft.learner import provider_learner
from app.bot.keyboards import CB_FB_DISLIKE_PREFIX, CB_FB_LIKE_PREFIX
from app.database import (
    feedback_col,
    locations_col,
    notification_history_col,
)

logger = logging.getLogger(__name__)


async def handle_feedback_callback(update: Update) -> None:
    """Process callback query for notification like/dislike buttons."""
    query = update.callback_query
    if query is None or query.data is None:
        return

    await query.answer()  # Acknowledge immediately
    user = update.effective_user
    if user is None:
        return

    data = query.data
    user_id = user.id

    if data.startswith(CB_FB_LIKE_PREFIX):
        notif_id = data[len(CB_FB_LIKE_PREFIX):]
        await _on_like(query, user_id, notif_id)
    elif data.startswith(CB_FB_DISLIKE_PREFIX):
        notif_id = data[len(CB_FB_DISLIKE_PREFIX):]
        await _on_dislike(query, user_id, notif_id)


async def _on_like(query: Any, user_id: int, notif_id: str) -> None:
    """User liked the notification."""
    await feedback_col().update_one(
        {"user_id": user_id, "notification_id": notif_id},
        {
            "$set": {
                "user_id": user_id,
                "notification_id": notif_id,
                "feedback": "like",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )

    if query.message:
        await query.message.reply_text(
            "👍 Thank you! Your feedback helps keep alerts accurate.",
            parse_mode=ParseMode.HTML,
        )


async def _on_dislike(query: Any, user_id: int, notif_id: str) -> None:
    """User disliked the notification — trigger re-learning + AI investigation."""
    await feedback_col().update_one(
        {"user_id": user_id, "notification_id": notif_id},
        {
            "$set": {
                "user_id": user_id,
                "notification_id": notif_id,
                "feedback": "dislike",
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )

    # Get user location for geohash
    loc = await locations_col().find_one({"user_id": user_id})
    geohash = loc.get("geohash", "") if loc else ""

    # Trigger re-learning (+25 test planes)
    if geohash:
        await provider_learner.trigger_relearning(
            user_id=user_id, geohash=geohash, extra_planes=25
        )

    # Lookup notification details for AI investigation
    notif_doc = await notification_history_col().find_one(
        {"_id": notif_id}
    ) or await notification_history_col().find_one(
        {"user_id": user_id, "aircraft_icao24": notif_id}
    )

    ai_analysis = ""
    if notif_doc and ai_judge.can_call():
        try:
            icao = notif_doc.get("aircraft_icao24", notif_id)
            ac_type = notif_doc.get("aircraft_type", "Unknown")
            dist = notif_doc.get("distance_km", 0.0)
            providers = notif_doc.get("reporting_providers", ["all"])

            ai_analysis = await ai_judge.analyze_dislike(
                icao24=icao,
                aircraft_type=ac_type,
                distance_km=dist,
                providers_reporting=providers,
                user_feedback="User marked this alert as not helpful or wrong",
            )
            logger.info("AI dislike analysis for user %d: %s", user_id, ai_analysis)
        except Exception as exc:
            logger.debug("AI dislike analysis error: %s", exc)

    reply_msg = (
        "🔄 <b>Feedback recorded!</b>\n\n"
        "We'll run extra provider checks (+25 test planes) to improve accuracy in your area.\n"
    )
    if ai_analysis and ai_analysis != "UNKNOWN":
        reply_msg += f"\n🤖 <i>AI Analysis: {ai_analysis}</i>"

    if query.message:
        await query.message.reply_text(reply_msg, parse_mode=ParseMode.HTML)


from app.intelligence.diagnostics_v6 import (  # noqa: E402
    V6_FEEDBACK_LABELS,
    normalize_v6_feedback_reason,
)
