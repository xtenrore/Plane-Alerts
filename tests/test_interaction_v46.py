import asyncio
from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from app.bot import interaction_v46 as iv46
from app.bot import next60


class FakeMessage:
    def __init__(self, events):
        self.events = events

    async def reply_text(self, text, **kwargs):
        self.events.append(("reply", text))
        return None


class FakeQuery:
    def __init__(self, query_id, data, events):
        self.id = query_id
        self.data = data
        self.message = FakeMessage(events)
        self.events = events

    async def answer(self, text=None, **kwargs):
        self.events.append(("answer", text))
        return True


def make_update(query_id, events):
    return SimpleNamespace(
        callback_query=FakeQuery(query_id, f"{next60.MORE_PREFIX}THY123", events),
        effective_user=SimpleNamespace(id=42),
    )


@pytest.fixture(autouse=True)
def _reset():
    iv46.clear_latency_state_for_tests()
    yield
    iv46.clear_latency_state_for_tests()


def test_next60_callback_acknowledges_before_slow_database_work(monkeypatch):
    events = []

    async def slow_docs(user_id, now):
        events.append(("db_start", user_id))
        await asyncio.sleep(0.01)
        events.append(("db_done", user_id))
        return []

    monkeypatch.setattr(next60, "build_next60_docs", slow_docs)
    update = make_update("q-fast-ack", events)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(iv46.next60_more_v46(update, None))

    names = [item[0] for item in events]
    assert names[0] == "answer"
    assert names.index("answer") < names.index("db_start") < names.index("db_done")
    assert names[-1] == "reply"


def test_duplicate_callback_does_not_repeat_database_or_message_work(monkeypatch):
    events = []
    calls = 0

    async def docs(user_id, now):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(next60, "build_next60_docs", docs)
    first = make_update("same-query", events)
    second = make_update("same-query", events)

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(iv46.next60_more_v46(first, None))
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(iv46.next60_more_v46(second, None))

    assert calls == 1
    answers = [item for item in events if item[0] == "answer"]
    assert answers[0] == ("answer", None)
    assert answers[-1] == ("answer", "Already handled.")
    replies = [item for item in events if item[0] == "reply"]
    assert len(replies) == 1


def test_profile_state_cache_avoids_repeated_slow_reads():
    calls = 0

    async def read_state(user_id):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "profile:aircraft", {"draft": user_id}

    first = asyncio.run(iv46._cached_state_from(read_state, 123))
    second = asyncio.run(iv46._cached_state_from(read_state, 123))
    assert calls == 1
    assert first == second
    # Returned state must be a copy so one handler cannot mutate the cache used
    # by a later button press.
    second[1]["draft"] = "changed"
    third = asyncio.run(iv46._cached_state_from(read_state, 123))
    assert third[1]["draft"] == 123


def test_latency_snapshot_reports_p50_p95_p99_and_worst():
    iv46._metrics["profiles"]["ack_ms"].extend([1.0, 2.0, 3.0, 4.0, 20.0])
    snapshot = iv46.latency_snapshot("profiles")["profiles"]["ack_ms"]
    assert snapshot["count"] == 5
    assert snapshot["p50"] == pytest.approx(3.0)
    assert snapshot["p95"] is not None
    assert snapshot["p99"] is not None
    assert snapshot["worst"] == 20.0


def test_distinct_rapid_callbacks_are_not_mistaken_for_duplicates():
    q1 = SimpleNamespace(id="rapid-1")
    q2 = SimpleNamespace(id="rapid-2")
    assert iv46._is_duplicate(q1) is False
    assert iv46._is_duplicate(q2) is False
    assert iv46._is_duplicate(q1) is True
    assert iv46._is_duplicate(q2) is True
