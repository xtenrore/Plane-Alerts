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


SUPERVISOR_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plane Alerts · Supervisor Chat</title><style>
:root{color-scheme:dark;--bg:#0b0f14;--panel:#121820;--line:#26313d;--muted:#8fa0b2;--text:#e9eef4;--accent:#7cb8e8;--good:#7bc49b;--bad:#df8585}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{display:flex;align-items:center;gap:12px;padding:14px 18px;border-bottom:1px solid var(--line);background:#0e1319}.brand{font-weight:700}.brand small{display:block;color:var(--muted);font-weight:500;font-size:11px}.spacer{flex:1}a,button{color:var(--text)}a{color:var(--accent);text-decoration:none}main{max-width:1440px;margin:auto;padding:18px}.layout{display:grid;grid-template-columns:250px minmax(0,1fr) 330px;gap:12px;align-items:stretch}.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;min-width:0}.sidebar,.tracepanel{height:calc(100vh - 98px);min-height:560px;overflow:hidden;display:flex;flex-direction:column}.sidehead{padding:12px;border-bottom:1px solid var(--line);font-weight:650}.historylist,.trace{overflow:auto;padding:8px}.historyitem{width:100%;text-align:left;border:1px solid transparent;background:transparent;padding:9px;border-radius:6px;margin:0 0 5px;color:var(--text)}.historyitem:hover,.historyitem.active{background:#17202a;border-color:var(--line)}.historytitle{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.historymeta{display:block;font-size:11px;color:var(--muted);margin-top:3px}.toolbar{display:flex;gap:8px;align-items:center;padding:12px;border-bottom:1px solid var(--line);flex-wrap:wrap}.status{color:var(--muted);font-size:12px}.chat{height:calc(100vh - 310px);min-height:380px;overflow:auto;padding:16px;display:grid;gap:12px}.message{max-width:86%;padding:11px 13px;border:1px solid var(--line);border-radius:8px;white-space:pre-wrap;overflow-wrap:anywhere}.user{justify-self:end;background:#172433}.assistant{justify-self:start;background:#10161d}.meta{font-size:11px;color:var(--muted);margin-top:6px}.composer{display:grid;grid-template-columns:1fr auto;gap:8px;padding:12px;border-top:1px solid var(--line)}textarea{min-height:70px;resize:vertical;background:#0d1218;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:10px;font:inherit}button{border:1px solid var(--line);background:#17202a;border-radius:7px;padding:9px 12px;cursor:pointer}button:disabled{opacity:.55;cursor:default}.note{padding:10px 12px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--line)}.bad{color:var(--bad)}.good{color:var(--good)}.traceitem{border-bottom:1px solid var(--line);padding:9px 4px}.traceitem:last-child{border-bottom:0}.tracekind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}.tracetitle{font-weight:600;margin-top:2px}.tracedetail{font-size:12px;color:var(--muted);white-space:pre-wrap;overflow-wrap:anywhere;margin-top:4px}.empty{padding:12px;color:var(--muted);font-size:12px}@media(max-width:1050px){.layout{grid-template-columns:220px minmax(0,1fr)}.tracepanel{grid-column:1/-1;height:auto;min-height:0;max-height:360px}}@media(max-width:700px){main{padding:10px}.layout{grid-template-columns:1fr}.sidebar,.tracepanel{height:auto;min-height:0;max-height:300px}.chat{height:52vh;min-height:340px}.message{max-width:95%}.composer{grid-template-columns:1fr}.toolbar{align-items:flex-start}}</style></head><body>
<header><div class="brand">Plane Alerts<small>Private AI Operations · Supervisor Chat</small></div><div class="spacer"></div><a href="/">Operations dashboard</a></header>
<main><div class="layout"><aside class="panel sidebar"><div class="sidehead">Chat history</div><div id="historyList" class="historylist"><div class="empty">Loading conversations…</div></div></aside><section class="panel"><div class="toolbar"><button id="new">New conversation</button><label><input id="switch" type="checkbox"> ask another provider</label><span id="status" class="status">Checking owner session…</span></div><div class="note">Grounded read-only operations chat. Missing evidence is reported as unavailable. The Supervisor cannot merge, deploy, change secrets/configuration, or decide flight/alert behavior.</div><div id="chat" class="chat"><div class="status">No conversation loaded.</div></div><form id="form" class="composer"><textarea id="message" maxlength="8192" placeholder="Ask about current tasks, findings, provider health, tests, replays, Git artifacts, or backups…" required></textarea><button id="send" type="submit">Send</button></form></section><aside class="panel tracepanel"><div class="sidehead">Reasoning trace</div><div class="note">Exact observable execution history: provider attempts, failovers, read-only tools, and persisted continuity. Private hidden chain-of-thought is not stored or exposed.</div><div id="trace" class="trace"><div class="empty">Select a conversation to view its trace.</div></div></aside></div></main>
<script>
let csrf='',conversationId=localStorage.getItem('plane_alerts_supervisor_conversation')||'';const chat=document.querySelector('#chat'),statusEl=document.querySelector('#status'),send=document.querySelector('#send'),historyList=document.querySelector('#historyList'),trace=document.querySelector('#trace');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=v=>{const d=new Date(Number(v||0)*1000);return Number.isFinite(d.getTime())?d.toLocaleString():''};
async function api(path,opt={}){opt.headers={...(opt.headers||{}),'Accept':'application/json'};if(opt.body&&!opt.headers['Content-Type'])opt.headers['Content-Type']='application/json';if(opt.method&&opt.method!=='GET')opt.headers['X-CSRF-Token']=csrf;const r=await fetch(path,opt);if(r.status===401)throw Error('owner authentication required');const body=await r.text();let value={};try{value=body?JSON.parse(body):{}}catch(_){throw Error('invalid server response')}if(!r.ok)throw Error(value.error||('HTTP '+r.status));return value}
function setStatus(text,cls=''){statusEl.className='status '+cls;statusEl.textContent=text}
function render(history){const messages=history?.messages||[];if(!messages.length){chat.innerHTML='<div class="status">Start a grounded operations conversation.</div>';return}chat.innerHTML=messages.map(m=>`<div class="message ${m.role==='user'?'user':'assistant'}">${esc(m.content)}<div class="meta">${esc(m.role)}${m.created?' · '+esc(when(m.created)):''}</div></div>`).join('');chat.scrollTop=chat.scrollHeight}
function renderHistory(items){if(!items.length){historyList.innerHTML='<div class="empty">No saved conversations yet.</div>';return}historyList.innerHTML=items.map(c=>`<button class="historyitem ${c.conversation_id===conversationId?'active':''}" data-id="${esc(c.conversation_id)}"><span class="historytitle">${esc(c.title||'New conversation')}</span><span class="historymeta">${esc(c.status||'ACTIVE')} · ${esc(c.message_count)} messages${c.updated?' · '+esc(when(c.updated)):''}</span></button>`).join('');historyList.querySelectorAll('.historyitem').forEach(btn=>btn.onclick=async()=>{conversationId=btn.dataset.id||'';localStorage.setItem('plane_alerts_supervisor_conversation',conversationId);await load();await loadHistory()})}
function jsonText(v){try{return JSON.stringify(v,null,2)}catch(_){return String(v??'')}}
function renderTrace(data){const items=[];(data.usage||[]).forEach(x=>items.push({t:x.created,k:'provider',title:`${x.success?'Success':'Failure'} · ${x.provider} · ${x.model}`,detail:`${x.slot||''}${x.failure_kind?' · '+x.failure_kind:''} · ${x.latency_ms} ms · in ${x.input_tokens} / out ${x.output_tokens}${x.estimated_neurons?' · '+x.estimated_neurons+' neurons':''}`}));(data.switches||[]).forEach(x=>items.push({t:x.created,k:'failover',title:`${x.from_provider||'none'} → ${x.to_provider||'unknown'}`,detail:`${x.reason||'route change'}${x.from_model?' · '+x.from_model:''}${x.to_model?' → '+x.to_model:''}`}));(data.tools||[]).forEach(x=>items.push({t:x.created,k:'tool',title:x.tool,detail:`arguments ${jsonText(x.arguments)}\nresult ${jsonText(x.result)}\nhash ${x.result_hash}`}));items.sort((a,b)=>(a.t||0)-(b.t||0));if(data.continuity){items.push({t:data.conversation?.updated||0,k:'continuity',title:'Persisted continuity',detail:jsonText(data.continuity)})}if(!items.length){trace.innerHTML='<div class="empty">No provider/tool activity has been recorded for this conversation yet.</div>';return}trace.innerHTML=items.map(x=>`<div class="traceitem"><div class="tracekind">${esc(x.k)}${x.t?' · '+esc(when(x.t)):''}</div><div class="tracetitle">${esc(x.title)}</div><div class="tracedetail">${esc(x.detail)}</div></div>`).join('')}
async function loadHistory(){const r=await api('/api/supervisor/conversations?limit=50');renderHistory(r.conversations||[])}
async function session(){try{const s=await api('/api/session');csrf=s.csrf;const st=await api('/api/supervisor/status');setStatus(st.enabled?`Ready · ${st.primary_provider} · ${st.model}`:'Supervisor disabled',st.enabled?'good':'bad');await loadHistory();if(conversationId)await load()}catch(e){setStatus(e.message+' · sign in on the Operations dashboard first','bad');send.disabled=true}}
async function createConversation(){const r=await api('/api/supervisor/conversations',{method:'POST',body:'{}'});conversationId=r.conversation_id;localStorage.setItem('plane_alerts_supervisor_conversation',conversationId);render({messages:[]});renderTrace({});await loadHistory();return conversationId}
async function load(){if(!conversationId)return;try{const [h,t]=await Promise.all([api('/api/supervisor/history?conversation_id='+encodeURIComponent(conversationId)),api('/api/supervisor/trace?conversation_id='+encodeURIComponent(conversationId))]);render(h);renderTrace(t);if(h.conversation?.provider)setStatus(`Ready · ${h.conversation.provider} · ${h.conversation.model||'model unavailable'}`,'good')}catch(_){conversationId='';localStorage.removeItem('plane_alerts_supervisor_conversation');render({messages:[]});renderTrace({})}}
document.querySelector('#new').onclick=async()=>{try{await createConversation();setStatus('New conversation ready','good')}catch(e){setStatus(e.message,'bad')}};
document.querySelector('#form').onsubmit=async e=>{e.preventDefault();send.disabled=true;try{if(!conversationId)await createConversation();let text=document.querySelector('#message').value.trim();if(!text)return;if(document.querySelector('#switch').checked)text='Switch provider. '+text;document.querySelector('#message').value='';const r=await api('/api/supervisor/chat',{method:'POST',body:JSON.stringify({conversation_id:conversationId,message:text})});await load();await loadHistory();setStatus(`${r.status} · ${r.provider||'degraded'} · ${r.model||'no model'}${r.slot?' · '+r.slot:''}`,r.status==='OK'?'good':'bad')}catch(e){setStatus(e.message,'bad')}finally{send.disabled=false}};
session();
</script></body></html>'''


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
