"""Final Phase 8B grounded Supervisor acceptance tests."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.private_ops.provider_adapters import ProviderFailure, Slot
from app.private_ops.store import Store
from app.private_ops.supervisor import (
    ChatResult, SupervisorBackend, SupervisorEngine, SupervisorStore,
    CLOUDFLARE_DAILY_NEURON_BUDGET,
)


class FakeTransport:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def chat(self, slot, model, messages, *, tools=(), affinity=""):
        self.calls.append((slot.provider, slot.name, model, affinity, list(messages)))
        action = self.behavior(slot, model, messages, tools)
        if isinstance(action, Exception):
            raise action
        return action


def _seed(tmp_path):
    store = Store(tmp_path)
    store.enqueue("task:live")
    db = store.db
    db.execute("""INSERT INTO ai_ops_findings(
        finding_id,signature,occurrence,status,classification,severity,subsystem,case_ref,created,updated)
        VALUES('finding:one','sig',1,'OPEN','INVESTIGATE','MEDIUM','prediction','case:one',1,2)""")
    db.execute("""INSERT OR REPLACE INTO ai_ops_provider_health(
        slot,provider,attempts,successes,failures,input_tokens,output_tokens,consecutive_failures,cooldown_until,last_status,updated)
        VALUES('GROQ_KEY','groq',3,2,1,10,5,1,0,'timeout',2)""")
    store.close()
    return SupervisorBackend(tmp_path / "ai_ops.sqlite"), SupervisorStore(tmp_path)


def _slots():
    return [
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "cf1", "a" * 32),
        Slot("cloudflare", "CLOUDFLARE_API_TOKEN_2", "cf2", "b" * 32),
        Slot("groq", "GROQ_KEY", "g1"),
        Slot("gemini", "GEMINI_API_KEY", "gm1"),
        Slot("openrouter", "OPENROUTER_API", "or1"),
    ]


def test_backend_grounding_and_missing_records(tmp_path):
    backend, _state = _seed(tmp_path)
    assert backend.get_task("task:live")["available"] is True
    missing = backend.get_task("task:missing")
    assert missing == {"available": False, "reason": "task_not_found", "task_id": "task:missing"}
    assert backend.get_finding("finding:one")["available"] is True
    assert backend.get_deployments() == {"available": False, "reason": "deployment_records_not_persisted_in_ai_ops_backend"}
    assert backend.get_replay_results()["available"] is True
    snapshot = json.dumps(backend.snapshot("show task:live and finding:one")).lower()
    assert "task:live" in snapshot and "finding:one" in snapshot
    assert "password" not in snapshot and "authorization" not in snapshot


def test_conversation_persists_without_hidden_reasoning(tmp_path):
    _backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-persist")
    state.append_message(cid, "user", "What happened this hour?")
    state.append_message(cid, "assistant", "One durable task is currently pending.")
    state.set_affinity(cid, "cloudflare", "@cf/zai-org/glm-4.7-flash", "CLOUDFLARE_API_TOKEN", reason="initial_selection")
    state.update_continuity(cid, {"verified_facts": ["task:live"], "pending_action": None}, "snapshot-1")

    reopened = SupervisorStore(tmp_path)
    convo = reopened.conversation(cid)
    assert convo["provider"] == "cloudflare"
    assert convo["model"] == "@cf/zai-org/glm-4.7-flash"
    assert reopened.messages(cid)[-1]["content"] == "One durable task is currently pending."
    raw = sqlite3.connect(tmp_path / "supervisor.sqlite").execute(
        "SELECT continuity_json FROM supervisor_conversations WHERE conversation_id=?", (cid,)
    ).fetchone()[0]
    assert "chain_of_thought" not in raw.lower() and "reasoning_content" not in raw.lower()


def test_cloudflare_slot1_to_slot2_failover_becomes_session_affinity(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-failover")

    def behavior(slot, _model, _messages, _tools):
        if slot.name == "CLOUDFLARE_API_TOKEN":
            return ProviderFailure("quota", status=429, retry_after=60, provider_wide=True)
        return ChatResult("Current durable state shows task:live.", (), 100, 20)

    transport = FakeTransport(behavior)
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    first = engine.chat(cid, "What happened this hour?")
    assert first["status"] == "OK" and first["provider"] == "cloudflare" and first["slot"] == "Slot 2"
    assert [call[1] for call in transport.calls[:2]] == ["CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_2"]

    transport.calls.clear()
    second = engine.chat(cid, "What is still active?")
    assert second["status"] == "OK" and transport.calls[0][1] == "CLOUDFLARE_API_TOKEN_2"
    assert state.conversation(cid)["slot"] == "CLOUDFLARE_API_TOKEN_2"
    switches = state.switch_history(cid)
    assert any(item["reason"] == "provider_failover" for item in switches)


def test_fallback_provider_after_both_cloudflare_slots_fail(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-fallback")

    def behavior(slot, _model, _messages, _tools):
        if slot.provider == "cloudflare":
            return ProviderFailure("timeout")
        if slot.provider == "groq":
            return ChatResult("Backend evidence is available and task:live is pending.", (), 50, 12)
        return ProviderFailure("server")

    transport = FakeTransport(behavior)
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    result = engine.chat(cid, "Show current tasks")
    assert result["status"] == "OK" and result["provider"] == "groq"
    assert [call[0] for call in transport.calls[:3]] == ["cloudflare", "cloudflare", "groq"]


def test_all_provider_failure_is_honest_degraded_mode(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-degraded")
    transport = FakeTransport(lambda *_: ProviderFailure("server"))
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    result = engine.chat(cid, "What happened?")
    assert result["status"] == "DEGRADED"
    assert "no model reasoning was performed" in result["answer"].lower()
    assert state.conversation(cid)["status"] == "DEGRADED"


def test_hallucinated_operational_id_is_not_accepted(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-hallucination")
    transport = FakeTransport(lambda *_: ChatResult("I found task:invented and it passed.", (), 20, 10))
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    result = engine.chat(cid, "What happened?")
    assert "do not have durable backend evidence" in result["answer"].lower()
    assert "it passed" not in result["answer"].lower()


def test_secret_like_user_message_never_reaches_provider(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-secret")
    transport = FakeTransport(lambda *_: ChatResult("should not run", (), 1, 1))
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    result = engine.chat(cid, "password=do-not-send-this")
    assert result["status"] == "REJECTED"
    assert transport.calls == []
    assert state.messages(cid) == []


def test_read_tool_round_trip_is_persisted_and_grounded(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-tools")
    count = {"n": 0}

    def behavior(_slot, _model, _messages, _tools):
        count["n"] += 1
        if count["n"] == 1:
            return ChatResult(None, ({
                "id": "call-1", "type": "function",
                "function": {"name": "get_task", "arguments": '{"task_id":"task:live"}'},
            },), 40, 5)
        return ChatResult("The backend confirms task:live is currently pending.", (), 60, 12)

    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=FakeTransport(behavior))
    result = engine.chat(cid, "Show task:live")
    assert result["status"] == "OK"
    db = sqlite3.connect(tmp_path / "supervisor.sqlite")
    tool = db.execute("SELECT tool,result_json FROM supervisor_tool_events WHERE conversation_id=?", (cid,)).fetchone()
    assert tool[0] == "get_task" and '"available":true' in tool[1]


def test_free_budget_guard_skips_cloudflare_before_paid_overage(tmp_path):
    backend, state = _seed(tmp_path)
    cid = state.create_conversation("sup-budget")
    with sqlite3.connect(tmp_path / "supervisor.sqlite") as db:
        db.execute("""INSERT INTO supervisor_usage(
            conversation_id,provider,model,slot,utc_day,input_tokens,output_tokens,estimated_neurons,success,latency_ms,created)
            VALUES(?,?,?,?,date('now'),0,0,?,1,1,1)""",
            (cid, "cloudflare", "@cf/zai-org/glm-4.7-flash", "Slot 1", CLOUDFLARE_DAILY_NEURON_BUDGET))
        db.execute("""INSERT INTO supervisor_usage(
            conversation_id,provider,model,slot,utc_day,input_tokens,output_tokens,estimated_neurons,success,latency_ms,created)
            VALUES(?,?,?,?,date('now'),0,0,?,1,1,1)""",
            (cid, "cloudflare", "@cf/zai-org/glm-4.7-flash", "Slot 2", CLOUDFLARE_DAILY_NEURON_BUDGET))
    transport = FakeTransport(lambda slot, *_: ChatResult("task:live remains pending.", (), 20, 5) if slot.provider == "groq" else ProviderFailure("server"))
    engine = SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash", transport=transport)
    result = engine.chat(cid, "Show current tasks")
    assert result["provider"] == "groq"
    assert all(call[0] != "cloudflare" for call in transport.calls)


def test_paid_cloudflare_and_openrouter_models_are_rejected(tmp_path):
    backend, state = _seed(tmp_path)
    with pytest.raises(ValueError, match="Workers-Free"):
        SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-5.2")
    with pytest.raises(ValueError, match="paid OpenRouter"):
        SupervisorEngine(backend, state, _slots(), cloudflare_model="@cf/zai-org/glm-4.7-flash",
                         fallback_models={"openrouter": "vendor/paid-model"})
