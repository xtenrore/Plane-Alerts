"""Regression coverage for unusable Supervisor provider responses."""
from __future__ import annotations

import sqlite3

import pytest

from app.private_ops.provider_adapters import ProviderFailure, Slot
from app.private_ops.store import Store
from app.private_ops.supervisor import ChatResult, SupervisorBackend, SupervisorEngine, SupervisorStore
from app.private_ops.supervisor_transport_guard import GuardedSupervisorTransport


class FakeDelegate:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def chat(self, slot, model, messages, *, tools=(), affinity=""):
        self.calls.append((slot.provider, slot.name, model, list(messages)))
        action = self.behavior(slot, model, messages, tools)
        if isinstance(action, Exception):
            raise action
        return action


def _seed(tmp_path):
    store = Store(tmp_path)
    store.enqueue("task:guard")
    store.close()
    return SupervisorBackend(tmp_path / "ai_ops.sqlite"), SupervisorStore(tmp_path)


def _slots():
    return [
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "cf1", "a" * 32),
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN_2", "cf2", "b" * 32),
        Slot("groq", "GROQ_KEY", "g1"),
    ]


def test_empty_provider_response_is_failure_not_success():
    guard = GuardedSupervisorTransport(FakeDelegate(lambda *_: ChatResult(None, (), 10, 0)))
    with pytest.raises(ProviderFailure) as caught:
        guard.chat(_slots()[0], "@cf/nvidia/nemotron-3-120b-a12b", [{"role": "user", "content": "status"}])
    assert caught.value.kind == "empty_response"


def test_whitespace_only_provider_response_is_failure():
    guard = GuardedSupervisorTransport(FakeDelegate(lambda *_: ChatResult("   ", (), 10, 1)))
    with pytest.raises(ProviderFailure) as caught:
        guard.chat(_slots()[0], "@cf/nvidia/nemotron-3-120b-a12b", [{"role": "user", "content": "status"}])
    assert caught.value.kind == "empty_response"


def test_tool_request_remains_valid_before_bound_is_exhausted():
    tool_call = ({
        "id": "call-1",
        "type": "function",
        "function": {"name": "get_current_tasks", "arguments": "{}"},
    },)
    guard = GuardedSupervisorTransport(FakeDelegate(lambda *_: ChatResult(None, tool_call, 10, 1)))
    result = guard.chat(_slots()[0], "@cf/nvidia/nemotron-3-120b-a12b", [{"role": "user", "content": "status"}])
    assert result.tool_calls == tool_call


def test_tool_loop_after_bounded_rounds_becomes_provider_failure():
    tool_call = ({
        "id": "call-next",
        "type": "function",
        "function": {"name": "get_current_tasks", "arguments": "{}"},
    },)
    messages = [
        {"role": "user", "content": "status"},
        {"role": "assistant", "content": None, "tool_calls": list(tool_call)},
        {"role": "tool", "tool_call_id": "call-1", "content": "{}"},
        {"role": "assistant", "content": None, "tool_calls": list(tool_call)},
        {"role": "tool", "tool_call_id": "call-2", "content": "{}"},
    ]
    guard = GuardedSupervisorTransport(FakeDelegate(lambda *_: ChatResult(None, tool_call, 10, 1)))
    with pytest.raises(ProviderFailure) as caught:
        guard.chat(_slots()[0], "@cf/nvidia/nemotron-3-120b-a12b", messages)
    assert caught.value.kind == "tool_round_exhausted"


def test_empty_primary_response_fails_over_and_persists_real_answer(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-empty-regression")

    def behavior(slot, _model, _messages, _tools):
        if slot.name == "CLOUDFLARE_API_TOKEN":
            return ChatResult(None, (), 40, 0)
        if slot.name == "CLOUDFLARE_API_TOKEN_2":
            return ChatResult("Durable backend evidence shows task:guard is pending.", (), 50, 12)
        return ProviderFailure("server")

    delegate = FakeDelegate(behavior)
    engine = SupervisorEngine(
        backend,
        state,
        _slots(),
        cloudflare_model="@cf/nvidia/nemotron-3-120b-a12b",
        transport=GuardedSupervisorTransport(delegate),
    )
    result = engine.chat(cid, "How are things looking? Have we found evidence?")

    assert result["status"] == "OK"
    assert result["provider"] == "cloudflare"
    assert result["slot"] == "Slot 2"
    assert result["answer"] == "Durable backend evidence shows task:guard is pending."
    assert [call[1] for call in delegate.calls[:2]] == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_2"]
    assert state.messages(cid)[-1]["content"] == result["answer"]

    with sqlite3.connect(tmp_path / "supervisor.sqlite") as db:
        failures = db.execute(
            "SELECT failure_kind FROM supervisor_usage WHERE conversation_id=? AND success=0 ORDER BY id",
            (cid,),
        ).fetchall()
    assert ("empty_response",) in failures
