from app.photography.keyboards import PHOTO_CALLBACK_PREFIX, notification_actions_keyboard


def test_alert_keyboard_has_photo_and_feedback_actions():
    kb = notification_actions_keyboard("notif123")
    assert len(kb.inline_keyboard) == 2
    assert kb.inline_keyboard[0][0].callback_data == f"{PHOTO_CALLBACK_PREFIX}notif123"
    assert kb.inline_keyboard[1][0].callback_data.startswith("fb:like:")
    assert kb.inline_keyboard[1][1].callback_data.startswith("fb:dis:")


def test_alert_keyboard_uses_flightradar24_fallback_when_only_icao_is_known():
    kb = notification_actions_keyboard("notif123", "4BAA01")
    assert len(kb.inline_keyboard) == 3
    assert kb.inline_keyboard[0][0].text == "Open in Flightradar24"
    assert kb.inline_keyboard[0][0].url == "https://www.flightradar24.com/data"
    assert kb.inline_keyboard[1][0].callback_data == f"{PHOTO_CALLBACK_PREFIX}notif123"
