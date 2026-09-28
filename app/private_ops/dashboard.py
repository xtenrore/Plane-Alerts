"""Authenticated, read-only Private AI Operations control-plane web UI.

The durable SQLite database remains the source of truth. Request handlers open short-lived
read-only connections so the HTTP server can safely serve concurrent requests without
sharing the Store writer connection across threads.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from http import cookies
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_BODY = 16_384
MAX_ROWS = 200
SESSION_COOKIE = "aiops_session"
LOGIN_CSRF_COOKIE = "aiops_login_csrf"
SENSITIVE_PARTS = (
    "token", "secret", "password", "authorization", "api_key", "apikey",
    "mongo_uri", "credential", "latitude", "longitude", "observer_lat",
    "observer_lon", "receiver_lat", "receiver_lon",
)
TERMINAL_FINDING_STATUSES = {
    "RESOLVED", "EXPECTED_BEHAVIOR", "INCONCLUSIVE", "DUPLICATE",
    "ALREADY_FIXED", "FALSE_POSITIVE", "WONT_FIX_WITH_REASON",
}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _safe(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return "[bounded]"
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for raw_key, item in list(value.items())[:200]:
            key = str(raw_key)
            lowered = key.lower()
            if any(part in lowered for part in SENSITIVE_PARTS):
                out[key] = "[redacted]"
            else:
                out[key] = _safe(item, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_safe(item, depth=depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return value if len(value) <= 4096 else value[:4096] + "…[truncated]"
    return value


def _loads(raw: str | None) -> object | None:
    if not raw:
        return None
    try:
        return _safe(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return "[unavailable]"


def _connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA busy_timeout=2000")
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def read_health(path: Path) -> dict[str, object]:
    with _connect(path) as db:
        tables = _tables(db)
        pending_ai = db.execute(
            "SELECT count(*) FROM ai_ops_cases WHERE state='PENDING_AI'"
        ).fetchone()[0] if "ai_ops_cases" in tables else 0
        active_findings = db.execute(
            "SELECT count(*) FROM ai_ops_findings WHERE status NOT IN "
            "('RESOLVED','EXPECTED_BEHAVIOR','INCONCLUSIVE','DUPLICATE',"
            "'ALREADY_FIXED','FALSE_POSITIVE','WONT_FIX_WITH_REASON')"
        ).fetchone()[0] if "ai_ops_findings" in tables else 0
        return {
            "schema": db.execute("PRAGMA user_version").fetchone()[0],
            "integrity": db.execute("PRAGMA quick_check").fetchone()[0],
            "pending": db.execute(
                "SELECT count(*) FROM ai_ops_jobs WHERE status IN ('PENDING','RETRY')"
            ).fetchone()[0],
            "pending_ai": pending_ai,
            "active_findings": active_findings,
        }


@dataclass
class Session:
    expires: float
    csrf: str
    created: float
    last_seen: float


class AuthState:
    def __init__(self, password: str, *, session_ttl: int = 1800) -> None:
        if not password:
            raise ValueError("owner password is required")
        if not 300 <= session_ttl <= 86_400:
            raise ValueError("session ttl outside safe bounds")
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self._ttl = session_ttl
        self._sessions: dict[str, Session] = {}
        self._login_attempts: dict[str, deque[float]] = defaultdict(deque)
        self._request_attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        expired = [sid for sid, session in self._sessions.items() if session.expires <= now]
        for sid in expired:
            self._sessions.pop(sid, None)
        if len(self._login_attempts) > 256:
            self._login_attempts.clear()
        if len(self._request_attempts) > 512:
            self._request_attempts.clear()

    @staticmethod
    def _window_allowed(bucket: deque[float], now: float, *, limit: int, window: int) -> bool:
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def request_allowed(self, address: str) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            return self._window_allowed(self._request_attempts[address], now, limit=180, window=60)

    def login(self, address: str, supplied: str) -> tuple[str, Session] | None:
        now = time.time()
        with self._lock:
            self._prune(now)
            if not self._window_allowed(self._login_attempts[address], now, limit=6, window=300):
                raise PermissionError("login throttled")
            supplied_digest = hashlib.sha256(supplied.encode("utf-8")).digest()
            if not hmac.compare_digest(supplied_digest, self._password_digest):
                return None
            self._login_attempts.pop(address, None)
            sid = secrets.token_urlsafe(32)
            session = Session(
                expires=now + self._ttl,
                csrf=secrets.token_urlsafe(24),
                created=now,
                last_seen=now,
            )
            self._sessions[sid] = session
            return sid, session

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(sid)
            if session is None or session.expires <= now:
                return None
            session.last_seen = now
            session.expires = now + self._ttl
            return session

    def logout(self, sid: str | None) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)


class EventBuffer:
    def __init__(self, maxlen: int = 64) -> None:
        self._events: deque[tuple[int, str, object]] = deque(maxlen=maxlen)
        self._cursor = int(time.time() * 1000)
        self._last_digest = ""
        self._lock = threading.Lock()

    def snapshot(self, payload: object) -> tuple[int, str, object] | None:
        encoded = _json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        with self._lock:
            if digest == self._last_digest:
                return None
            self._last_digest = digest
            self._cursor += 1
            item = (self._cursor, "snapshot", payload)
            self._events.append(item)
            return item

    def after(self, cursor: int) -> list[tuple[int, str, object]]:
        with self._lock:
            return [item for item in self._events if item[0] > cursor]


def _overview(path: Path) -> dict[str, object]:
    with _connect(path) as db:
        tables = _tables(db)
        health = read_health(path)
        now = time.time()
        active = db.execute(
            "SELECT count(*) FROM ai_ops_jobs WHERE status='RUNNING'"
        ).fetchone()[0]
        stale = db.execute(
            "SELECT count(*) FROM ai_ops_jobs WHERE status='RUNNING' AND lease_until IS NOT NULL AND lease_until<?",
            (now,),
        ).fetchone()[0]
        reviews = db.execute(
            "SELECT count(*) FROM ai_ops_cases WHERE state IN ('PENDING_REVIEW','REVIEW_PENDING','PENDING_AI')"
        ).fetchone()[0] if "ai_ops_cases" in tables else 0
        findings_hour = db.execute(
            "SELECT count(*) FROM ai_ops_findings WHERE created>=?", (now - 3600,)
        ).fetchone()[0] if "ai_ops_findings" in tables else 0
        findings_day = db.execute(
            "SELECT count(*) FROM ai_ops_findings WHERE created>=?", (now - 86400,)
        ).fetchone()[0] if "ai_ops_findings" in tables else 0
        scheduler = [
            dict(row) for row in db.execute(
                "SELECT name,checkpoint,updated FROM ai_ops_scheduler ORDER BY name LIMIT 50"
            )
        ]
        dr = [
            dict(row) for row in db.execute(
                "SELECT destination,revision,synced_revision,last_attempt,last_verified,last_hash,next_attempt,retry_count "
                "FROM ai_ops_dr_sync ORDER BY destination"
            )
        ] if "ai_ops_dr_sync" in tables else []
        latest_tool = dict(db.execute(
            "SELECT operation_id,tool,status,created,finished FROM ai_ops_tool_operations "
            "ORDER BY created DESC LIMIT 1"
        ).fetchone() or {}) if "ai_ops_tool_operations" in tables else {}
        return _safe({
            "service": {"health": health, "persistent_storage": health["integrity"] == "ok"},
            "queue_depth": health["pending"],
            "active_tasks": active,
            "stale_tasks": stale,
            "reviews_pending": reviews,
            "findings_hour": findings_hour,
            "findings_day": findings_day,
            "scheduler": scheduler,
            "dr": dr,
            "latest_tool": latest_tool,
            "generated_at": now,
        })


def _agents(path: Path) -> list[dict[str, object]]:
    with _connect(path) as db:
        rows = db.execute(
            "SELECT id,status,lease_owner,lease_until,created,updated FROM ai_ops_jobs "
            "WHERE status='RUNNING' ORDER BY updated DESC LIMIT ?", (MAX_ROWS,)
        ).fetchall()
        result: list[dict[str, object]] = []
        now = time.time()
        tables = _tables(db)
        for job in rows:
            step = db.execute(
                "SELECT step_id,status,attempts FROM ai_ops_steps WHERE job_id=? "
                "ORDER BY rowid DESC LIMIT 1", (job["id"],)
            ).fetchone()
            attempt = db.execute(
                "SELECT worker,started,ended,result FROM ai_ops_attempts WHERE job_id=? "
                "ORDER BY id DESC LIMIT 1", (job["id"],)
            ).fetchone()
            tool = db.execute(
                "SELECT operation_id,tool,status FROM ai_ops_tool_operations WHERE job_id=? "
                "ORDER BY created DESC LIMIT 1", (job["id"],)
            ).fetchone() if "ai_ops_tool_operations" in tables else None
            result.append(_safe({
                "worker_id": job["lease_owner"] or (attempt["worker"] if attempt else "unknown"),
                "role": step["step_id"] if step else "job",
                "current_task": job["id"],
                "provider": None,
                "model": None,
                "credential_slot": None,
                "status": job["status"],
                "start_time": attempt["started"] if attempt else job["created"],
                "elapsed_seconds": max(0, now - float(attempt["started"] if attempt else job["created"])),
                "current_step": dict(step) if step else None,
                "usage": None,
                "last_tool_operation": dict(tool) if tool else None,
                "provider_failure": None,
            }))
        return result


def _tasks(path: Path, limit: int = 100) -> list[dict[str, object]]:
    limit = max(1, min(limit, MAX_ROWS))
    with _connect(path) as db:
        return [
            _safe(dict(row)) for row in db.execute(
                "SELECT id,status,priority,created,updated,lease_owner,lease_until "
                "FROM ai_ops_jobs ORDER BY created DESC LIMIT ?", (limit,)
            )
        ]


def _task(path: Path, job_id: str) -> dict[str, object] | None:
    with _connect(path) as db:
        job = db.execute("SELECT * FROM ai_ops_jobs WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return None
        steps = [dict(row) for row in db.execute(
            "SELECT step_id,status,input_hash,output_hash,attempts,lease_owner,lease_until "
            "FROM ai_ops_steps WHERE job_id=? ORDER BY rowid", (job_id,)
        )]
        attempts = [dict(row) for row in db.execute(
            "SELECT id,step_id,worker,started,ended,result FROM ai_ops_attempts "
            "WHERE job_id=? ORDER BY id", (job_id,)
        )]
        tools = [dict(row) for row in db.execute(
            "SELECT operation_id,finding_id,tool,arguments_json,source_commit,sandbox_id,status,"
            "started,finished,result_class,result_json,output_hash,artifact_hash,created "
            "FROM ai_ops_tool_operations WHERE job_id=? ORDER BY created", (job_id,)
        )] if "ai_ops_tool_operations" in _tables(db) else []
        for item in tools:
            item["arguments"] = _loads(str(item.pop("arguments_json", "")))
            item["result"] = _loads(str(item.pop("result_json", "")))
        return _safe({"task": dict(job), "steps": steps, "attempts": attempts, "tool_operations": tools})


def _findings(path: Path, view: str, limit: int = 100) -> list[dict[str, object]]:
    limit = max(1, min(limit, MAX_ROWS))
    with _connect(path) as db:
        if "ai_ops_findings" not in _tables(db):
            return []
        placeholders = ",".join("?" for _ in TERMINAL_FINDING_STATUSES)
        params: list[object] = list(sorted(TERMINAL_FINDING_STATUSES))
        if view == "active":
            where = f"status NOT IN ({placeholders})"
        elif view == "history":
            where = f"status IN ({placeholders})"
        else:
            where = "1=1"
            params = []
        params.append(limit)
        rows = db.execute(
            f"SELECT finding_id,severity,status,classification,subsystem,case_ref,previous_finding_id,"
            f"fix_commit,fix_version,created,updated FROM ai_ops_findings WHERE {where} "
            "ORDER BY updated DESC LIMIT ?", params
        ).fetchall()
        return [_safe(dict(row)) for row in rows]


def _finding(path: Path, finding_id: str) -> dict[str, object] | None:
    with _connect(path) as db:
        tables = _tables(db)
        finding = db.execute("SELECT * FROM ai_ops_findings WHERE finding_id=?", (finding_id,)).fetchone()
        if finding is None:
            return None
        packets = [dict(row) for row in db.execute(
            "SELECT e.packet_id,e.window_start,e.case_ref,e.kind,e.content_hash,e.created "
            "FROM ai_ops_finding_packets fp JOIN ai_ops_evidence e ON e.packet_id=fp.packet_id "
            "WHERE fp.finding_id=? ORDER BY e.created", (finding_id,)
        )] if "ai_ops_finding_packets" in tables else []
        reviews = [dict(row) for row in db.execute(
            "SELECT review_id,packet_id,role,provider,model,key_slot,classification,severity,"
            "validation_status,agreement,disposition,independence,created "
            "FROM ai_ops_reviews WHERE packet_id IN "
            "(SELECT packet_id FROM ai_ops_finding_packets WHERE finding_id=?) ORDER BY created",
            (finding_id,),
        )] if "ai_ops_reviews" in tables else []
        feedback = db.execute(
            "SELECT verdict,updated FROM ai_ops_feedback WHERE finding_id=?", (finding_id,)
        ).fetchone() if "ai_ops_feedback" in tables else None
        tools = [dict(row) for row in db.execute(
            "SELECT operation_id,tool,source_commit,status,result_class,output_hash,artifact_hash,created,finished "
            "FROM ai_ops_tool_operations WHERE finding_id=? ORDER BY created", (finding_id,)
        )] if "ai_ops_tool_operations" in tables else []
        base = dict(finding)
        base["resolution"] = _loads(finding["resolution_json"]) if "resolution_json" in finding.keys() else None
        base.pop("resolution_json", None)
        return _safe({
            "finding": base, "evidence": packets, "reviews": reviews,
            "feedback": dict(feedback) if feedback else None, "tool_operations": tools,
        })


def _providers(path: Path) -> list[dict[str, object]]:
    with _connect(path) as db:
        tables = _tables(db)
        if "ai_ops_provider_health" not in tables:
            return []
        rows = db.execute(
            "SELECT slot,provider,attempts,successes,failures,input_tokens,output_tokens,"
            "consecutive_failures,cooldown_until,last_status,updated "
            "FROM ai_ops_provider_health ORDER BY provider,slot"
        ).fetchall()
        usage_exists = "ai_ops_usage" in tables
        result: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            if usage_exists:
                agg = db.execute(
                    "SELECT count(*) calls,coalesce(avg(latency_ms),0) latency,"
                    "coalesce(avg(CASE WHEN malformed=1 THEN 1.0 ELSE 0 END),0) malformed_rate,"
                    "max(created) last_call FROM ai_ops_usage WHERE provider=? AND key_slot=?",
                    (row["provider"], row["slot"]),
                ).fetchone()
                item["usage"] = dict(agg)
                item["last_successful_call"] = db.execute(
                    "SELECT max(created) FROM ai_ops_usage WHERE provider=? AND key_slot=? AND success=1",
                    (row["provider"], row["slot"]),
                ).fetchone()[0]
            result.append(_safe(item))
        return result


def _engineering(path: Path, limit: int = 100) -> list[dict[str, object]]:
    with _connect(path) as db:
        if "ai_ops_tool_operations" not in _tables(db):
            return []
        rows = db.execute(
            "SELECT operation_id,job_id,finding_id,tool,source_commit,sandbox_id,status,"
            "started,finished,result_class,result_json,output_hash,artifact_hash,created "
            "FROM ai_ops_tool_operations ORDER BY created DESC LIMIT ?", (max(1, min(limit, MAX_ROWS)),)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["result"] = _loads(str(item.pop("result_json", "")))
            out.append(_safe(item))
        return out


def _dr(path: Path) -> dict[str, object]:
    with _connect(path) as db:
        if "ai_ops_dr_sync" not in _tables(db):
            return {"github": {"status": "unavailable"}, "dropbox": {"status": "not_configured"}}
        destinations = {}
        for row in db.execute("SELECT * FROM ai_ops_dr_sync ORDER BY destination"):
            item = dict(row)
            item["status"] = "verified" if row["last_verified"] else "pending"
            item["lag_revisions"] = int(row["revision"]) - int(row["synced_revision"])
            destinations[str(row["destination"])] = item
        return _safe(destinations)


GUI_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Plane Alerts · Private Operations</title><style>
:root{color-scheme:dark;--bg:#0b0f14;--panel:#121820;--line:#26313d;--muted:#8fa0b2;--text:#e9eef4;--accent:#7cb8e8;--good:#7bc49b;--warn:#d8b66f;--bad:#df8585}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}header{position:sticky;top:0;z-index:4;display:flex;align-items:center;gap:18px;padding:14px 20px;border-bottom:1px solid var(--line);background:rgba(11,15,20,.96)}.brand{font-weight:700;letter-spacing:.02em}.brand small{display:block;color:var(--muted);font-weight:500;font-size:11px}.stale{margin-left:auto;color:var(--warn);font-size:12px}.stale.live{color:var(--good)}nav{display:flex;gap:6px;overflow:auto;border-bottom:1px solid var(--line);padding:8px 14px;background:#0e1319}button,.nav{border:1px solid var(--line);background:#151c24;color:var(--text);padding:8px 10px;border-radius:6px;cursor:pointer}.nav.active{border-color:#547999;background:#182533}main{padding:18px;max-width:1500px;margin:auto}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.card{background:var(--panel);border:1px solid var(--line);border-radius:7px;padding:14px;min-width:0}.metric{font-size:24px;font-variant-numeric:tabular-nums}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line)}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}td{overflow-wrap:anywhere}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.muted{color:var(--muted)}.good{color:var(--good)}.warn{color:var(--warn)}.bad{color:var(--bad)}.hidden{display:none}.state{padding:28px;text-align:center;color:var(--muted);border:1px dashed var(--line)}#login{max-width:380px;margin:14vh auto;padding:20px}.field{display:grid;gap:7px;margin:16px 0}input{width:100%;background:#0d1218;color:var(--text);border:1px solid var(--line);border-radius:6px;padding:11px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#0b1016;border:1px solid var(--line);padding:12px;border-radius:6px;max-height:420px;overflow:auto}@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:12px}header{padding:12px 14px}}@media(max-width:620px){.grid{grid-template-columns:1fr}nav{padding:7px 9px}.nav{white-space:nowrap}table,thead,tbody,tr,th,td{display:block}thead{display:none}tr{border-bottom:1px solid var(--line);padding:8px}td{border:0;padding:5px 7px}td:before{content:attr(data-label);display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.metric{font-size:21px}}
</style></head><body>
<section id="login" class="card hidden"><div class="brand">Plane Alerts<small>Private AI Operations</small></div><p class="muted">Owner authentication required.</p><form id="loginForm"><div class="field"><label for="password">Password</label><input id="password" type="password" autocomplete="current-password" required></div><button type="submit">Sign in</button><p id="loginError" class="bad"></p></form></section>
<section id="app" class="hidden"><header><div class="brand">Plane Alerts<small>Private AI Operations</small></div><span id="health" class="muted">Loading…</span><span id="stream" class="stale">STALE</span><button id="logout">Logout</button></header><nav id="nav"></nav><main id="content"><div class="state">Loading current durable state…</div></main></section>
<script>
const pages=["Overview","Supervisor Chat","Live Agents","Tasks","Findings","Providers","Engineering","Recovery","Events"];let csrf="",active="Overview",events=[];const esc=v=>String(v??"—").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));async function api(path,opt={}){opt.headers={...(opt.headers||{}),"Accept":"application/json"};if(opt.method&&opt.method!=="GET")opt.headers["X-CSRF-Token"]=csrf;let r=await fetch(path,opt);if(r.status===401){showLogin();throw Error("auth")};if(!r.ok)throw Error((await r.text()).slice(0,180));return r.json()}function showLogin(){document.querySelector("#app").classList.add("hidden");document.querySelector("#login").classList.remove("hidden")}function showApp(){document.querySelector("#login").classList.add("hidden");document.querySelector("#app").classList.remove("hidden")}function table(rows,cols){if(!rows.length)return'<div class="state">No records in current durable state.</div>';return'<table><thead><tr>'+cols.map(c=>`<th>${esc(c[0])}</th>`).join("")+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(c=>`<td data-label="${esc(c[0])}">${c[1](r)}</td>`).join("")+'</tr>').join("")+'</tbody></table>'}function j(v){return `<pre>${esc(JSON.stringify(v,null,2))}</pre>`}
async function render(){let c=document.querySelector("#content");c.innerHTML='<div class="state">Loading current durable state…</div>';try{if(active==="Overview"){let x=await api("/api/overview");document.querySelector("#health").textContent=`schema ${x.service.health.schema} · ${x.service.health.integrity}`;c.innerHTML='<div class="grid">'+[["Queue",x.queue_depth],["Active tasks",x.active_tasks],["Active findings",x.service.health.active_findings],["Reviews pending",x.reviews_pending],["Findings · 1h",x.findings_hour],["Findings · 24h",x.findings_day],["Stale tasks",x.stale_tasks],["Storage",x.service.persistent_storage?"healthy":"degraded"]].map(m=>`<div class="card"><div class="label">${esc(m[0])}</div><div class="metric">${esc(m[1])}</div></div>`).join("")+'</div><h3>Scheduler / DR</h3>'+j({scheduler:x.scheduler,dr:x.dr,latest_tool:x.latest_tool})}else if(active==="Supervisor Chat"){let chat=await api("/api/chat/session");c.innerHTML=`<div class="card"><h3>Supervisor Chat (${esc(chat.session.session_provider)} / ${esc(chat.session.session_model)})</h3><div id="chatMsgs" style="max-height:400px;overflow:auto;display:flex;flex-direction:column;gap:10px;margin-bottom:15px">${chat.messages.map(m=>`<div style="background:#151c24;padding:10px;border-radius:6px"><strong>${esc(m.role)}:</strong> ${esc(m.content)}${m.tool_calls_json?`<br><small class="muted">Tools: ${esc(m.tool_calls_json)}</small>`:""}</div>`).join("")}</div><form id="chatForm" style="display:flex;gap:10px"><input id="chatInput" placeholder="Ask Supervisor…" style="flex:1" required><button type="submit">Send</button></form></div>`;document.querySelector("#chatForm").onsubmit=async e=>{e.preventDefault();let inp=document.querySelector("#chatInput");let msg=inp.value;inp.value="";await api("/api/chat/message",{method:"POST",body:JSON.stringify({message:msg})});render()}}else if(active==="Live Agents"){let x=await api("/api/agents");c.innerHTML=table(x,[["Worker",r=>`<span class="mono">${esc(r.worker_id)}</span>`],["Task",r=>esc(r.current_task)],["Step",r=>esc(r.role)],["Provider",r=>esc(r.provider)],["Model",r=>esc(r.model)],["Slot",r=>esc(r.credential_slot)],["Status",r=>esc(r.status)],["Elapsed",r=>`${Math.round(r.elapsed_seconds)}s`],["Last tool",r=>esc(r.last_tool_operation?.tool)]])}else if(active==="Tasks"){let x=await api("/api/tasks?limit=200");c.innerHTML=table(x,[["Task",r=>`<span class="mono">${esc(r.id)}</span>`],["Status",r=>esc(r.status)],["Priority",r=>esc(r.priority)],["Worker",r=>esc(r.lease_owner)],["Updated",r=>new Date(r.updated*1000).toLocaleString()]])}else if(active==="Findings"){let a=await api("/api/findings?view=active&limit=200"),h=await api("/api/findings?view=history&limit=50");let cols=[["Finding",r=>`<span class="mono">${esc(r.finding_id)}</span>`],["Severity",r=>esc(r.severity)],["Status",r=>esc(r.status)],["Class",r=>esc(r.classification)],["Subsystem",r=>esc(r.subsystem)],["Updated",r=>new Date(r.updated*1000).toLocaleString()]];c.innerHTML="<h3>Active</h3>"+table(a,cols)+"<h3>History</h3>"+table(h,cols)}else if(active==="Providers"){let x=await api("/api/providers");c.innerHTML=table(x,[["Provider",r=>esc(r.provider)],["Slot",r=>`<span class="mono">${esc(r.slot)}</span>`],["Health",r=>esc(r.last_status)],["Success / fail",r=>`${esc(r.successes)} / ${esc(r.failures)}`],["Cooldown",r=>r.cooldown_until>Date.now()/1000?"active":"ready"],["Latency",r=>`${Math.round(r.usage?.latency||0)} ms`],["Last success",r=>r.last_successful_call?new Date(r.last_successful_call*1000).toLocaleString():"—"]])}else if(active==="Engineering"){let x=await api("/api/engineering?limit=100");c.innerHTML=table(x,[["Operation",r=>`<span class="mono">${esc(r.operation_id)}</span>`],["Tool",r=>esc(r.tool)],["Status",r=>esc(r.status)],["Source",r=>`<span class="mono">${esc(r.source_commit)}</span>`],["Result",r=>esc(r.result_class)],["Artifact",r=>`<span class="mono">${esc(r.artifact_hash)}</span>`]])}else if(active==="Recovery"){c.innerHTML=j(await api("/api/dr"))}else if(active==="Events"){c.innerHTML=events.length?events.slice(-100).reverse().map(e=>`<div class="card"><span class="mono">${esc(e.id)}</span> ${esc(e.type)} ${esc(new Date(e.at).toLocaleTimeString())}</div>`).join(""):'<div class="state">No live changes observed yet.</div>'}}catch(e){if(e.message!=="auth")c.innerHTML=`<div class="state bad">Unable to load current state: ${esc(e.message)}</div>`}}
function buildNav(){let n=document.querySelector("#nav");n.innerHTML=pages.map(p=>`<button class="nav ${p===active?"active":""}" data-p="${p}">${p}</button>`).join("");n.onclick=e=>{let p=e.target.dataset.p;if(p){active=p;buildNav();render()}}}function connect(){let s=new EventSource("/api/events/stream");s.onopen=()=>{let e=document.querySelector("#stream");e.textContent="LIVE";e.classList.add("live")};s.onmessage=e=>{events.push({id:e.lastEventId,type:"snapshot",at:Date.now(),data:JSON.parse(e.data)});if(active==="Overview"||active==="Events")render()};s.onerror=()=>{let e=document.querySelector("#stream");e.textContent="STALE";e.classList.remove("live")}}document.querySelector("#loginForm").onsubmit=async e=>{e.preventDefault();let p=document.querySelector("#password");try{let b=await api("/api/auth/bootstrap");let r=await fetch("/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":b.csrf},body:JSON.stringify({password:p.value})});p.value="";if(!r.ok){document.querySelector("#loginError").textContent=r.status===429?"Too many attempts. Try again later.":"Sign-in failed.";return}let x=await r.json();csrf=x.csrf;showApp();buildNav();render();connect()}catch(x){document.querySelector("#loginError").textContent="Sign-in unavailable."}};document.querySelector("#logout").onclick=async()=>{try{await api("/api/auth/logout",{method:"POST"})}finally{csrf="";location.reload()}};(async()=>{try{let x=await api("/api/session");csrf=x.csrf;showApp();buildNav();render();connect()}catch(e){showLogin()}})();
</script></body></html>'''


def create_dashboard_handler(
    db_path: Path,
    *,
    password: str,
    require_https: bool = True,
    session_ttl: int = 1800,
    stream_interval: float = 5.0,
    stream_max_seconds: float = 55.0,
) -> type[BaseHTTPRequestHandler]:
    auth = AuthState(password, session_ttl=session_ttl)
    events = EventBuffer()

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "PlaneAlertsPrivateOps"

        def _client(self) -> str:
            forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            return forwarded or self.client_address[0]

        def _cookies(self) -> cookies.SimpleCookie[str]:
            jar: cookies.SimpleCookie[str] = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                pass
            return jar

        def _session_id(self) -> str | None:
            morsel = self._cookies().get(SESSION_COOKIE)
            return morsel.value if morsel else None

        def _session(self) -> Session | None:
            return auth.get(self._session_id())

        def _secure_request(self) -> bool:
            if not require_https:
                return True
            return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return origin in {f"https://{host}", f"http://{host}"}

        def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
                  extra: list[tuple[str, str]] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            if extra:
                for key, value in extra:
                    self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, value: object, *, extra: list[tuple[str, str]] | None = None) -> None:
            self._send(status, _json_bytes(_safe(value)), extra=extra)

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _body(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length < 0 or length > MAX_BODY:
                raise ValueError("request body too large")
            data = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
            return data

        def _require_session(self) -> Session | None:
            session = self._session()
            if session is None:
                self._error(401, "authentication required")
            return session

        def _csrf_ok(self, session: Session) -> bool:
            supplied = self.headers.get("X-CSRF-Token", "")
            return bool(supplied) and hmac.compare_digest(supplied, session.csrf) and self._same_origin()

        def _route_get(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/health", "/ready"):
                try:
                    status = read_health(db_path)
                    ready = status["integrity"] == "ok"
                except Exception:
                    status, ready = {"status": "unavailable"}, False
                self._json(200 if ready else 503, status)
                return
            if not self._secure_request():
                self._error(400, "TLS required")
                return
            if not auth.request_allowed(self._client()):
                self._error(429, "rate limit")
                return
            if path == "/api/auth/bootstrap":
                token = secrets.token_urlsafe(24)
                cookie = f"{LOGIN_CSRF_COOKIE}={token}; Path=/; Max-Age=300; Secure; SameSite=Strict"
                self._json(200, {"csrf": token}, extra=[("Set-Cookie", cookie)])
                return
            if path == "/":
                self._send(200, GUI_HTML.encode(), "text/html; charset=utf-8")
                return
            session = self._require_session()
            if session is None:
                return
            if path == "/api/session":
                self._json(200, {"csrf": session.csrf, "expires": session.expires})
                return
            try:
                query = parse_qs(parsed.query)
                if path == "/api/overview": self._json(200, _overview(db_path))
                elif path == "/api/agents": self._json(200, _agents(db_path))
                elif path == "/api/tasks": self._json(200, _tasks(db_path, int(query.get("limit", ["100"])[0])))
                elif path.startswith("/api/tasks/"):
                    item = _task(db_path, path.rsplit("/", 1)[-1]); self._json(200, item) if item else self._error(404, "task not found")
                elif path == "/api/findings": self._json(200, _findings(db_path, query.get("view", ["active"])[0], int(query.get("limit", ["100"])[0])))
                elif path.startswith("/api/findings/"):
                    item = _finding(db_path, path.rsplit("/", 1)[-1]); self._json(200, item) if item else self._error(404, "finding not found")
                elif path == "/api/providers": self._json(200, _providers(db_path))
                elif path == "/api/engineering": self._json(200, _engineering(db_path, int(query.get("limit", ["100"])[0])))
                elif path == "/api/dr": self._json(200, _dr(db_path))
                elif path == "/api/chat/session":
                    from .store import Store
                    from .supervisor import SupervisorEngine
                    st = Store(db_path.parent)
                    try:
                        sup = SupervisorEngine(st)
                        sess = sup.get_or_create_session(query.get("conversation_id", [None])[0])
                        msgs = st.list_chat_messages(sess["session_id"])
                        self._json(200, {"session": sess, "messages": msgs})
                    finally:
                        st.close()
                elif path == "/api/events":
                    cursor = int(query.get("cursor", ["0"])[0]); events.snapshot(_overview(db_path)); self._json(200, [{"id": i, "type": typ, "data": data} for i, typ, data in events.after(cursor)])
                elif path == "/api/events/stream": self._stream_events()
                else: self._error(404, "not found")
            except (ValueError, sqlite3.Error):
                self._error(400, "invalid request")

        def _stream_events(self) -> None:
            try: cursor = int(self.headers.get("Last-Event-ID", "0") or "0")
            except ValueError: cursor = 0
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store"); self.send_header("Connection", "keep-alive"); self.send_header("X-Accel-Buffering", "no"); self.end_headers()
            deadline = time.time() + stream_max_seconds
            try:
                while time.time() < deadline:
                    events.snapshot(_overview(db_path)); pending = events.after(cursor)
                    if pending:
                        for item_id, _typ, data in pending:
                            self.wfile.write(f"id: {item_id}\ndata: {_json_bytes(_safe(data)).decode()}\n\n".encode()); cursor = item_id
                    else: self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush(); time.sleep(stream_interval)
            except (BrokenPipeError, ConnectionResetError): return

        def do_GET(self) -> None: self._route_get()
        def do_HEAD(self) -> None: self._route_get()

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if not self._secure_request(): self._error(400, "TLS required"); return
            if not auth.request_allowed(self._client()): self._error(429, "rate limit"); return
            if parsed.path == "/api/auth/login":
                jar = self._cookies(); morsel = jar.get(LOGIN_CSRF_COOKIE); cookie_csrf = morsel.value if morsel else ""; header_csrf = self.headers.get("X-CSRF-Token", "")
                if not cookie_csrf or not header_csrf or not hmac.compare_digest(cookie_csrf, header_csrf) or not self._same_origin(): self._error(403, "csrf rejected"); return
                try:
                    body = self._body(); supplied = body.get("password", "")
                    if not isinstance(supplied, str): raise ValueError("invalid password")
                    result = auth.login(self._client(), supplied)
                except PermissionError: self._error(429, "login throttled"); return
                except (ValueError, json.JSONDecodeError): self._error(400, "invalid request"); return
                if result is None: self._error(401, "authentication failed"); return
                sid, session = result
                session_cookie = f"{SESSION_COOKIE}={sid}; Path=/; Max-Age={int(session.expires-time.time())}; Secure; HttpOnly; SameSite=Strict"
                clear_login = f"{LOGIN_CSRF_COOKIE}=; Path=/; Max-Age=0; Secure; SameSite=Strict"
                self._json(200, {"csrf": session.csrf, "expires": session.expires}, extra=[("Set-Cookie", session_cookie), ("Set-Cookie", clear_login)]); return
            session = self._require_session()
            if session is None: return
            if not self._csrf_ok(session): self._error(403, "csrf rejected"); return
            if parsed.path == "/api/auth/logout":
                auth.logout(self._session_id()); clear = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"; self._json(200, {"ok": True}, extra=[("Set-Cookie", clear)]); return
            if parsed.path == "/api/chat/message":
                from .store import Store
                from .supervisor import SupervisorEngine
                body = self._body()
                cid = str(body.get("conversation_id", "default-supervisor-session"))
                text = str(body.get("message", ""))
                if not text.strip():
                    self._error(400, "message content required")
                    return
                st = Store(db_path.parent)
                try:
                    sup = SupervisorEngine(st)
                    res = sup.process_user_message(cid, text)
                    self._json(200, res)
                finally:
                    st.close()
                return
            if parsed.path == "/api/approval/request":
                body = self._body()
                action = str(body.get("action", ""))
                diff_summary = str(body.get("diff_summary", ""))
                approved = bool(body.get("approved", False))
                if not action or not approved:
                    self._error(400, "explicit owner approval required for development/release mutation")
                    return
                self._json(200, {"status": "APPROVED", "action": action, "diff_summary": diff_summary, "timestamp": time.time()})
                return
            if parsed.path == "/api/chat/action":
                from .store import Store
                from .supervisor import SupervisorEngine
                body = self._body()
                action_type = str(body.get("action_type", ""))
                args = body.get("args", {})
                confirmed = bool(body.get("confirmed", False))
                if not action_type:
                    self._error(400, "action_type required")
                    return
                st = Store(db_path.parent)
                try:
                    sup = SupervisorEngine(st)
                    res = sup.execute_action(action_type, args, confirmed=confirmed)
                    self._json(200, res)
                finally:
                    st.close()
                return
            self._error(404, "not found")

        def log_message(self, format: str, *args: object) -> None:
            return

    return DashboardHandler
