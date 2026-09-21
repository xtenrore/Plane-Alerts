from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

import app.bot.profile_legacy as legacy
from app.version import VERSION


class Message:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


@pytest.mark.asyncio
async def test_status_uses_active_profile_summary_not_legacy_selector(monkeypatch):
    message = Message()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=5), message=message)

    class Users:
        async def find_one(self, query, projection=None):
            return {"user_id": 5, "setup_complete": True}

    async def active(user_id):
        return {
            "user_id": user_id,
            "profile_id": "0123456789",
            "name": "Photography",
            "config": {
                "location": {"radius_km": 8},
                "preferences": {
                    "aircraft_filter": {
                        "mode": "selected",
                        "selected_categories": ["widebody"],
                        "selected_types": ["A359"],
                        "excluded_types": [],
                    },
                    "filter_rules": {"profile": {}, "categories": {}, "aircraft": {}},
                },
            },
        }

    monkeypatch.setattr(legacy, "users_col", lambda: Users())
    monkeypatch.setattr(legacy, "ensure_default_profile", active)

    with pytest.raises(ApplicationHandlerStop):
        await legacy.cmd_status_profiled(update, SimpleNamespace())

    text = message.sent[0][0]
    assert "Photography" in text
    assert "8 km" in text
    assert "widebody" in text.casefold()


@pytest.mark.asyncio
async def test_help_exposes_profiles_and_advanced_preferences():
    message = Message()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=5), message=message)

    with pytest.raises(ApplicationHandlerStop):
        await legacy.cmd_help_profiled(update, SimpleNamespace())

    text = message.sent[0][0]
    assert "/profiles" in text
    assert "/preferences" in text
    assert f"v{VERSION}" in text
    assert "v4.3" not in text
