from pymongo.errors import AutoReconnect

from app.bot.storage_failure_v48 import _STORAGE_MESSAGE, _is_storage_error


def test_v48_storage_errors_are_classified_without_masking_unrelated_failures():
    assert _is_storage_error(AutoReconnect("temporary Mongo outage")) is True
    assert _is_storage_error(RuntimeError("Database not initialised – call connect_db() first.")) is True
    assert _is_storage_error(ValueError("unrelated handler failure")) is False


def test_v48_storage_failure_message_is_honest_about_live_monitoring():
    assert _STORAGE_MESSAGE == (
        "Settings are temporarily unavailable. Live aircraft monitoring is still running."
    )
