"""Authenticated web surface for the grounded Private AI Operations Supervisor.

This layer reuses the Phase 8A owner session and CSRF boundary. It exposes only
Supervisor conversation state and read-only grounded chat; it has no production
mutation, deployment, secret-management, or flight-decision endpoints.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from typing import Type
from urllib.parse import parse_qs, urlparse

from .supervisor import SupervisorEngine
from .supervisor_web_ui import SUPERVISOR_HTML


class _ChatThrottle:
    """Small in-process authenticated request throttle in addition to provider budgets."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allowed(self, key: str, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= current - 60:
                bucket.popleft()
            if len(bucket) >= 12:
                return False
            bucket.append(current)
            if len(self._events) > 256:
                self._events = defaultdict(deque, {key: bucket})
            return True


def _slot_label(provider: object, slot: object) -> str | None:
    provider_s, slot_s = str(provider or ""), str(slot or "")
    if not slot_s:
        return None
    if provider_s == "cloudflare":
        return "Slot 2" if slot_s.endswith("_2") else "Slot 1"
    return "fallback"


def _safe_conversation(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    out = dict(value)
    out["slot"] = _slot_label(out.get("provider"), out.get("slot"))
    return out


def _safe_switches(items: list[dict[str, object]]) -> list[dict[str, object]]:
    result = []
    for raw in items[-50:]:
        item = dict(raw)
        item["from_slot"] = _slot_label(item.get("from_provider"), item.get("from_slot"))
        item["to_slot"] = _slot_label(item.get("to_provider"), item.get("to_slot"))
        result.append(item)
    return result


def _safe_result(value: dict[str, object]) -> dict[str, object]:
    out = dict(value)
    if isinstance(out.get("switch_history"), list):
        out["switch_history"] = _safe_switches(out["switch_history"])
    return out


def _read_rows(path, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA busy_timeout=2000")
        return [dict(row) for row in db.execute(sql, params).fetchall()]
    finally:
        db.close()


def _conversation_index(supervisor: SupervisorEngine, limit: int) -> list[dict[str, object]]:
    bounded = max(1, min(int(limit), 100))
    rows = _read_rows(
        supervisor.state.path,
        """SELECT c.conversation_id,c.provider,c.model,c.slot,c.status,c.created,c.updated,
                  (SELECT content FROM supervisor_messages m WHERE m.conversation_id=c.conversation_id AND m.role='user' ORDER BY m.id LIMIT 1) AS first_message,
                  (SELECT count(*) FROM supervisor_messages m2 WHERE m2.conversation_id=c.conversation_id) AS message_count
           FROM supervisor_conversations c ORDER BY c.updated DESC LIMIT ?""",
        (bounded,),
    )
    result = []
    for row in rows:
        item = dict(row)
        first = str(item.pop("first_message") or "").strip()
        item["title"] = (first[:117] + "…") if len(first) > 120 else first
        item["slot"] = _slot_label(item.get("provider"), item.get("slot"))
        result.append(item)
    return result


def _trace_payload(supervisor: SupervisorEngine, conversation_id: str) -> dict[str, object]:
    conversation = supervisor.state.conversation(conversation_id)
    if conversation is None:
        raise KeyError("unknown conversation")
    tools = _read_rows(
        supervisor.state.path,
        "SELECT tool,arguments_json,result_json,result_hash,created FROM supervisor_tool_events WHERE conversation_id=? ORDER BY id LIMIT 100",
        (conversation_id,),
    )
    safe_tools = []
    for row in tools:
        try:
            arguments = json.loads(str(row.get("arguments_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            arguments = {"unavailable": True}
        try:
            result = json.loads(str(row.get("result_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            result = {"unavailable": True}
        safe_tools.append({
            "tool": str(row.get("tool") or ""),
            "arguments": arguments,
            "result": result,
            "result_hash": str(row.get("result_hash") or ""),
            "created": row.get("created"),
        })
    usage = _read_rows(
        supervisor.state.path,
        "SELECT provider,model,slot,input_tokens,output_tokens,estimated_neurons,success,failure_kind,latency_ms,created FROM supervisor_usage WHERE conversation_id=? ORDER BY id LIMIT 100",
        (conversation_id,),
    )
    for item in usage:
        item["slot"] = _slot_label(item.get("provider"), item.get("slot"))
        item["success"] = bool(item.get("success"))
    return {
        "conversation": _safe_conversation(conversation),
        "continuity": conversation.get("continuity", {}),
        "tools": safe_tools,
        "usage": usage,
        "switches": _safe_switches(supervisor.state.switch_history(conversation_id)),
        "trace_kind": "observable_execution_not_hidden_chain_of_thought",
    }


def extend_private_handler(
    base_handler: Type[BaseHTTPRequestHandler],
    supervisor: SupervisorEngine,
) -> Type[BaseHTTPRequestHandler]:
    """Add authenticated Supervisor routes to the already-secured Phase 8A handler."""
    throttle = _ChatThrottle()

    class SupervisorWebHandler(base_handler):
        def _supervisor_session(self):
            if not self._secure_request():
                self._error(400, "TLS required")
                return None
            return self._require_session()

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/supervisor":
                if not self._secure_request():
                    self._error(400, "TLS required")
                    return
                self._send(200, SUPERVISOR_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if not path.startswith("/api/supervisor/"):
                super().do_GET()
                return
            session = self._supervisor_session()
            if session is None:
                return
            if path == "/api/supervisor/status":
                providers = []
                for slot in supervisor.slots:
                    if slot.provider not in providers:
                        providers.append(slot.provider)
                self._json(200, {
                    "enabled": True,
                    "primary_provider": "cloudflare" if "cloudflare" in providers else (providers[0] if providers else "unavailable"),
                    "providers": providers,
                    "model": supervisor.cloudflare_model,
                    "authority": "read_only_operations",
                })
                return
            if path == "/api/supervisor/conversations":
                query = parse_qs(parsed.query)
                try:
                    limit = int(str(query.get("limit", ["50"])[0]))
                except ValueError:
                    limit = 50
                self._json(200, {"conversations": _conversation_index(supervisor, limit)})
                return
            if path == "/api/supervisor/history":
                query = parse_qs(parsed.query)
                conversation_id = str(query.get("conversation_id", [""])[0])
                conversation = supervisor.state.conversation(conversation_id)
                if conversation is None:
                    self._error(404, "conversation not found")
                    return
                self._json(200, {
                    "conversation": _safe_conversation(conversation),
                    "messages": supervisor.state.messages(conversation_id, limit=100),
                    "switch_history": _safe_switches(supervisor.state.switch_history(conversation_id)),
                })
                return
            if path == "/api/supervisor/trace":
                query = parse_qs(parsed.query)
                conversation_id = str(query.get("conversation_id", [""])[0])
                try:
                    self._json(200, _trace_payload(supervisor, conversation_id))
                except KeyError:
                    self._error(404, "conversation not found")
                return
            self._error(404, "not found")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if not path.startswith("/api/supervisor/"):
                super().do_POST()
                return
            session = self._supervisor_session()
            if session is None:
                return
            if not self._csrf_ok(session):
                self._error(403, "csrf rejected")
                return
            sid = self._session_id() or "authenticated"
            if not throttle.allowed(sid):
                self._error(429, "Supervisor request rate limit")
                return
            try:
                body = self._body()
                if path == "/api/supervisor/conversations":
                    conversation_id = supervisor.new_conversation()
                    self._json(201, {"conversation_id": conversation_id})
                    return
                if path == "/api/supervisor/chat":
                    conversation_id = str(body.get("conversation_id", ""))
                    message = body.get("message", "")
                    if not conversation_id:
                        conversation_id = supervisor.new_conversation()
                    if not isinstance(message, str):
                        raise ValueError("message must be text")
                    result = supervisor.chat(conversation_id, message)
                    payload = _safe_result(result)
                    payload["conversation_id"] = conversation_id
                    self._json(200, payload)
                    return
                self._error(404, "not found")
            except KeyError:
                self._error(404, "conversation not found")
            except (ValueError, json.JSONDecodeError, sqlite3.Error):
                self._error(400, "invalid Supervisor request")

    return SupervisorWebHandler
