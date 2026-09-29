"""Final Phase 8C confirmation-gated Supervisor safe-action acceptance tests."""
from __future__ import annotations

import json
import sqlite3
import time

from app.private_ops.provider_adapters import Slot
from app.private_ops.store import Store
from app.private_ops.supervisor import ChatResult, SupervisorStore
from app.private_ops.supervisor_actions import (
    ACTIONS,
    ActionSupervisorBackend,
    ActionSupervisorEngine,
    SafeActionController,
)


SOURCE = "a" * 40


def _seed(tmp_path):
    store = Store(tmp_path)
    store.enqueue("task:live", priority=1)
    store.enqueue("task:failed", priority=2)
    store.db.execute("UPDATE ai_ops_jobs SET status='FAILED' WHERE id='task:failed'")
    store.db.execute("""INSERT INTO ai_ops_findings(
        finding_id,signature,occurrence,status,classification,severity,subsystem,case_ref,created,updated)
        VALUES('finding:one','sig-action',1,'OPEN','INVESTIGATE','MEDIUM','prediction','case:one',1,2)""")
    packet = json.dumps({
        "schema": 2, "case_ref": "case:one", "kind": "test", "events": [],
    }, sort_keys=True, separators=(",", ":"))
    import hashlib
    packet_id = hashlib.sha256(packet.encode()).hexdigest()
    store.db.execute(
        """INSERT INTO ai_ops_evidence(packet_id,window_start,case_ref,kind,packet_json,content_hash,created)
           VALUES(?,1,'case:one','test',?,?,1)""",
        (packet_id, packet, packet_id),
    )
    store.db.execute(
        """INSERT INTO ai_ops_cases(packet_id,state,pending_role,created,updated)
           VALUES(?,'PENDING_AI','triage',1,1)""",
        (packet_id,),
    )
    store.db.execute(
        "INSERT INTO ai_ops_finding_packets(finding_id,packet_id,created) VALUES('finding:one',?,1)",
        (packet_id,),
    )
    store.close()
    state = SupervisorStore(tmp_path)
    cid = state.create_conversation("sup-actions")
    return cid, packet_id


def _backend(tmp_path):
    cid, packet_id = _seed(tmp_path)
    return cid, packet_id, ActionSupervisorBackend(
        tmp_path / "ai_ops.sqlite", source_commit=SOURCE
    )


def _propose(backend, cid, text, **request):
    with backend.bind(cid, text):
        result = backend.call("propose_safe_action", request)
    assert result["available"] is True
    assert result["confirmation_required"] is True
    return result["action"]["action_id"]


def _confirm(backend, cid, action_id):
    with backend.bind(cid, f"confirm {action_id}"):
        return backend.call("confirm_safe_action", {"action_id": action_id})


def test_action_allowlist_has_no_production_or_flight_mutation():
    assert {
        "merge", "deploy", "set_secret", "set_config", "set_trajectory", "set_cpa",
        "set_eta", "qualify_alert", "cancel_alert",
    }.isdisjoint(ACTIONS)


def test_proposal_requires_authenticated_bound_chat_and_does_not_mutate(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    rejected = backend.call(
        "propose_safe_action",
        {"action": "retry_failed_task", "target_id": "task:failed"},
    )
    assert rejected == {"available": False, "reason": "no_active_authenticated_chat_context"}

    action_id = _propose(
        backend, cid, "Please retry task:failed",
        action="retry_failed_task", target_id="task:failed",
    )
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute("SELECT status FROM ai_ops_jobs WHERE id='task:failed'").fetchone()[0] == "FAILED"
    with backend.bind(cid, f"confirm something-else, not {action_id.replace('action:', 'proposal:')}"):
        no_id = backend.call("confirm_safe_action", {"action_id": action_id})
    assert no_id["reason"] == "exact_action_id_not_in_owner_message"


def test_confirmation_is_explicit_idempotent_and_backend_owned(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    action_id = _propose(
        backend, cid, "Retry the failed task",
        action="retry_failed_task", target_id="task:failed",
    )
    with backend.bind(cid, action_id):
        missing = backend.call("confirm_safe_action", {"action_id": action_id})
    assert missing["reason"] == "explicit_owner_confirmation_missing"

    first = _confirm(backend, cid, action_id)
    assert first["available"] is True
    assert first["action"]["status"] == "SUCCEEDED"
    assert first["action"]["result"]["status"] == "RETRY"

    second = _confirm(backend, cid, action_id)
    assert second["action"] == first["action"]
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute("SELECT status FROM ai_ops_jobs WHERE id='task:failed'").fetchone()[0] == "RETRY"


def test_stale_action_and_user_cancel_are_fail_closed(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    stale = _propose(
        backend, cid, "Pause task:live later",
        action="pause_job", target_id="task:live",
    )
    with sqlite3.connect(tmp_path / "supervisor.sqlite") as db:
        db.execute("UPDATE supervisor_actions SET expires=? WHERE action_id=?", (time.time() - 1, stale))
        db.commit()
    expired = _confirm(backend, cid, stale)
    assert expired["action"]["status"] == "EXPIRED"
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute("SELECT status FROM ai_ops_jobs WHERE id='task:live'").fetchone()[0] == "PENDING"

    cancelled = _propose(
        backend, cid, "Maybe reprioritize task:live",
        action="reprioritize_queue", target_id="task:live", priority=50,
    )
    with backend.bind(cid, f"cancel {cancelled}"):
        result = backend.call("cancel_safe_action", {"action_id": cancelled})
    assert result["action"]["status"] == "CANCELLED"
    assert _confirm(backend, cid, cancelled)["action"]["status"] == "CANCELLED"


def test_pause_resume_cancel_and_reprioritize_are_bounded(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    pause = _propose(
        backend, cid, "Pause task:live",
        action="pause_job", target_id="task:live",
    )
    assert _confirm(backend, cid, pause)["action"]["result"]["status"] == "PAUSED"

    resume = _propose(
        backend, cid, "Resume task:live",
        action="resume_job", target_id="task:live",
    )
    assert _confirm(backend, cid, resume)["action"]["result"]["status"] == "RETRY"

    priority = _propose(
        backend, cid, "Raise task:live to priority 42",
        action="reprioritize_queue", target_id="task:live", priority=42,
    )
    assert _confirm(backend, cid, priority)["action"]["result"]["priority"] == 42

    cancel = _propose(
        backend, cid, "Cancel task:live",
        action="cancel_job", target_id="task:live",
    )
    assert _confirm(backend, cid, cancel)["action"]["result"]["status"] == "CANCELLED"
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        row = db.execute(
            "SELECT status,priority FROM ai_ops_jobs WHERE id='task:live'"
        ).fetchone()
    assert row == ("CANCELLED", 42)


def test_replay_and_test_actions_only_queue_exact_commit_isolated_operations(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    replay = _propose(
        backend, cid, "Run Error Museum replay for task:live",
        action="run_replay", target_id="task:live", replay_case="error_museum",
    )
    replay_result = _confirm(backend, cid, replay)["action"]
    assert replay_result["status"] == "QUEUED"
    assert replay_result["result"]["status"] == "QUEUED_FOR_ISOLATED_RUNNER"

    test = _propose(
        backend, cid, "Run targeted private review tests for task:live",
        action="run_targeted_tests", target_id="task:live", test_target="private_review",
    )
    test_result = _confirm(backend, cid, test)["action"]
    assert test_result["status"] == "QUEUED"

    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        rows = db.execute(
            "SELECT tool,source_commit,status,arguments_json FROM ai_ops_tool_operations ORDER BY created"
        ).fetchall()
    assert [row[0] for row in rows] == ["run_replay", "run_test"]
    assert all(row[1] == SOURCE and row[2] == "QUEUED" for row in rows)
    assert json.loads(rows[0][3]) == {"case": "error_museum"}
    assert json.loads(rows[1][3]) == {"candidate": None, "target": "private_review"}


def test_pause_cancels_pending_tool_operations_so_runner_cannot_ignore_job_pause(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    replay = _propose(
        backend, cid, "Run replay for task:live",
        action="run_replay", target_id="task:live", replay_case="prediction_lab",
    )
    _confirm(backend, cid, replay)
    pause = _propose(
        backend, cid, "Pause task:live now",
        action="pause_job", target_id="task:live",
    )
    result = _confirm(backend, cid, pause)["action"]["result"]
    assert result["cancelled_tool_operations"] == 1
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        status, result_class = db.execute(
            "SELECT status,result_class FROM ai_ops_tool_operations"
        ).fetchone()
    assert (status, result_class) == ("CANCELLED", "PAUSED_BY_OWNER")


def test_investigation_and_candidate_actions_link_finding_to_isolated_queue(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    investigation = _propose(
        backend, cid, "Start investigating finding:one",
        action="start_investigation", target_id="finding:one",
    )
    first = _confirm(backend, cid, investigation)["action"]
    assert first["status"] == "QUEUED"
    assert first["result"]["job_id"].startswith("task:investigate:")
    assert first["result"]["status"] == "QUEUED_FOR_ISOLATED_RUNNER"

    candidate = _propose(
        backend, cid, "Create isolated candidate investigation for finding:one",
        action="create_isolated_candidate_investigation", target_id="finding:one",
    )
    second = _confirm(backend, cid, candidate)["action"]
    assert second["status"] == "QUEUED"
    assert len(second["result"]["operation_ids"]) == 2

    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute(
            "SELECT status FROM ai_ops_findings WHERE finding_id='finding:one'"
        ).fetchone()[0] == "INVESTIGATING"
        assert db.execute(
            "SELECT count(*) FROM ai_ops_tool_operations WHERE finding_id='finding:one'"
        ).fetchone()[0] == 3


def test_request_reviewer_uses_original_evidence_queue_without_provider_call(tmp_path):
    cid, packet_id, backend = _backend(tmp_path)
    action_id = _propose(
        backend, cid, "Request an independent reviewer for finding:one",
        action="request_reviewer", target_id="finding:one",
    )
    result = _confirm(backend, cid, action_id)["action"]
    assert result["status"] == "QUEUED"
    assert result["result"]["packet_id"] == packet_id
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        case = db.execute(
            "SELECT state,pending_role,escalation_reason,batch_id FROM ai_ops_cases WHERE packet_id=?",
            (packet_id,),
        ).fetchone()
        batch = db.execute(
            "SELECT role,status FROM ai_ops_batches WHERE batch_id=?", (case[3],)
        ).fetchone()
    assert case[:3] == ("REVIEWING", "independent_review", "owner_requested_review")
    assert batch == ("independent_review", "PENDING")


def test_invalid_or_forbidden_action_never_creates_proposal(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    with backend.bind(cid, "Deploy it"):
        result = backend.call(
            "propose_safe_action",
            {"action": "deploy", "target_id": "task:live"},
        )
    assert result["available"] is False
    with sqlite3.connect(tmp_path / "supervisor.sqlite") as db:
        assert db.execute("SELECT count(*) FROM supervisor_actions").fetchone()[0] == 0


def test_proposal_survives_controller_restart_and_execution_is_not_auto_replayed(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    action_id = _propose(
        backend, cid, "Retry task:failed after restart",
        action="retry_failed_task", target_id="task:failed",
    )
    reopened = SafeActionController(tmp_path, source_commit=SOURCE)
    assert reopened.pending(cid)[0]["action_id"] == action_id
    assert reopened.pending(cid)[0]["status"] == "PROPOSED"


class ActionTransport:
    def __init__(self):
        self.action_id = None

    def chat(self, _slot, _model, messages, *, tools=(), affinity=""):
        assert any(
            item.get("function", {}).get("name") == "propose_safe_action"
            for item in tools
        )
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        if "retry task:failed" in str(last_user).lower() and self.action_id is None:
            return ChatResult(
                None,
                ({
                    "id": "call-propose", "type": "function",
                    "function": {
                        "name": "propose_safe_action",
                        "arguments": json.dumps({
                            "action": "retry_failed_task",
                            "target_id": "task:failed",
                        }),
                    },
                },),
                20,
                3,
            )
        if messages and messages[-1].get("role") == "tool":
            tool_result = json.loads(messages[-1]["content"])
            if "action" in tool_result and tool_result["action"].get("action_id"):
                action = tool_result["action"]
                self.action_id = action["action_id"]
                if action.get("status") == "PROPOSED":
                    return ChatResult(
                        f"Proposed {self.action_id}. Confirm using that exact action ID.",
                        (), 20, 8,
                    )
                return ChatResult("Safe action processed.", (), 20, 5)
            return ChatResult("Safe action request was rejected.", (), 20, 5)
        if self.action_id and self.action_id in str(last_user) and "confirm" in str(last_user).lower():
            return ChatResult(
                None,
                ({
                    "id": "call-confirm", "type": "function",
                    "function": {
                        "name": "confirm_safe_action",
                        "arguments": json.dumps({"action_id": self.action_id}),
                    },
                },),
                20,
                3,
            )
        return ChatResult("No safe action requested.", (), 20, 5)


def test_owner_can_propose_then_explicitly_confirm_action_through_chat(tmp_path):
    cid, _packet_id, backend = _backend(tmp_path)
    state = SupervisorStore(tmp_path)
    transport = ActionTransport()
    slot = Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "cf", "b" * 32)
    engine = ActionSupervisorEngine(
        backend,
        state,
        [slot],
        cloudflare_model="@cf/zai-org/glm-4.7-flash",
        transport=transport,
    )
    proposal = engine.chat(cid, "Please retry task:failed")
    assert proposal["status"] == "OK"
    assert transport.action_id in proposal["answer"]
    assert proposal["safe_actions"][0]["status"] == "PROPOSED"
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute("SELECT status FROM ai_ops_jobs WHERE id='task:failed'").fetchone()[0] == "FAILED"

    confirmed = engine.chat(cid, f"Confirm {transport.action_id}")
    assert confirmed["status"] == "OK"
    assert confirmed["safe_actions"][0]["status"] == "SUCCEEDED"
    with sqlite3.connect(tmp_path / "ai_ops.sqlite") as db:
        assert db.execute("SELECT status FROM ai_ops_jobs WHERE id='task:failed'").fetchone()[0] == "RETRY"


def test_backend_system_prompt_keeps_confirmation_and_production_boundaries(tmp_path):
    _cid, _packet_id, backend = _backend(tmp_path)
    state = SupervisorStore(tmp_path)
    slot = Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "cf", "b" * 32)
    engine = ActionSupervisorEngine(
        backend, state, [slot],
        cloudflare_model="@cf/zai-org/glm-4.7-flash",
        transport=ActionTransport(),
    )
    prompt = engine._system_prompt({})
    assert "exact action_id" in prompt
    assert "does not execute anything" in prompt
    assert "no merge/deploy/secret/config/flight-decision tool" in prompt
