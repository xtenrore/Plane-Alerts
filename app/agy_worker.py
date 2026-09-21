"""Private Antigravity supervisor for Plane Alerts Prediction Lab.

This process runs in its own Railway service. It deliberately has no Telegram
credentials and no Gemini API key. Account-based Antigravity authentication is
performed once through the private Telegram console exposed by the main app.

Safety invariants:
* AI credit overages are forcibly disabled before every AGY launch.
* API-key auth variables are stripped from AGY child processes.
* quota/rate-limit exits pause until the latest verified refresh + 10 minutes.
* unknown or ambiguous quota reset text never schedules an automatic retry.
* silence is not treated as failure: long reasoning is allowed. A separate
  heartbeat reports process liveness; only a very long no-output interval can
  trigger a watchdog restart.
* findings are persisted immediately to the mounted volume and, when MongoDB is
  configured, copied into the agy_findings collection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import signal
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("plane_alerts.agy_worker")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

STATE_DIR = Path(os.getenv("AGY_STATE_DIR", "/agy-state"))
HOME_DIR = STATE_DIR / "home"
WORKSPACE_DIR = Path(os.getenv("AGY_WORKSPACE_DIR", str(STATE_DIR / "workspace")))
FINDINGS_FILE = STATE_DIR / "prediction-lab" / "findings.jsonl"
SUPERVISOR_STATE_FILE = STATE_DIR / "prediction-lab" / "supervisor.json"
SETTINGS_FILE = HOME_DIR / ".gemini" / "antigravity-cli" / "settings.json"
WORKER_TOKEN = os.getenv("AGY_WORKER_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "8090"))

# The +10m guard is mandatory. If AGY does not provide an unambiguous reset
# deadline, the supervisor remains held until recovery is independently verified
# and the persisted state is explicitly cleared/advanced by an operator.
QUOTA_REFRESH_GUARD_SECONDS = int(os.getenv("AGY_QUOTA_REFRESH_GUARD_SECONDS", "600"))
WATCHDOG_SILENCE_SECONDS = int(os.getenv("AGY_WATCHDOG_SILENCE_SECONDS", "1200"))
GOAL_RETRY_SECONDS = int(os.getenv("AGY_GOAL_RETRY_SECONDS", "60"))
GOAL_CYCLE_SECONDS = int(os.getenv("AGY_GOAL_CYCLE_SECONDS", "3600"))

DEFAULT_GOAL = os.getenv(
    "AGY_GOAL",
    (
        "Continuously improve Plane Alerts prediction reliability using evidence from recorded "
        "predictions, actual outcomes, logs, tests, and replay cases. Never use paid AI credits. "
        "Do not make speculative production changes. For every confirmed issue, IMMEDIATELY run "
        "python /app/scripts/agy_record_finding.py with a concise severity, summary, evidence, and "
        "suggested fix so the finding is durable before continuing. Prefer deterministic/statistical "
        "prediction logic over runtime AI. Run tests for any candidate change. Do not deploy."
    ),
)

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
QUOTA_PATTERNS = (
    "quota exceeded",
    "rate limit",
    "resource exhausted",
    "baseline quota",
    "usage limit",
    "limit reached",
    "too many requests",
)
REFRESH_TS_RE = re.compile(
    r"(?:refresh(?:es|ed)?|reset(?:s)?)(?:\s+(?:at|on|in))?\s*[:=]?\s*"
    r"(?P<value>"
    r"\d{4}-\d{2}-\d{2}[T ][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})"
    r"|(?:\d+\s*(?:days?|hours?|minutes?|seconds?|d|h|m|s)(?:\s*(?:,|and)?\s*)?)+"
    r")",
    re.IGNORECASE,
)
DURATION_PART_RE = re.compile(r"(\d+)\s*(days?|hours?|minutes?|seconds?|d|h|m|s)\b", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean_terminal(text: str) -> str:
    text = ANSI_RE.sub("", text).replace("\r", "\n")
    text = CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip())


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _enforce_no_paid_credits() -> None:
    """Force account auth and permanently disable AI-credit overages."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_json(SETTINGS_FILE, {})
    settings.pop("modelProvider", None)
    settings["useG1Credits"] = False
    settings.setdefault("agentMode", "accept-edits")
    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    required_rules = [
        "command(git)",
        "command(pytest)",
        "command(python)",
        "command(regex:python /app/scripts/agy_record_finding.py.*)",
        "write_file(/agy-state/workspace/)",
        "write_file(/agy-state/prediction-lab/)",
    ]
    for rule in required_rules:
        if rule not in allow:
            allow.append(rule)
    _atomic_json(SETTINGS_FILE, settings)


def _agy_env() -> dict[str, str]:
    _enforce_no_paid_credits()
    env = os.environ.copy()
    env["HOME"] = str(HOME_DIR)
    env["PATH"] = f"/usr/local/bin:{HOME_DIR / '.local' / 'bin'}:" + env.get("PATH", "")
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    env["NO_COLOR"] = "1"
    env["SSH_CONNECTION"] = env.get("SSH_CONNECTION", "127.0.0.1 1 127.0.0.1 1")
    env["SSH_TTY"] = env.get("SSH_TTY", "/dev/pts/agy")
    for key in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        env.pop(key, None)
    return env


def _parse_duration_seconds(raw: str) -> int | None:
    parts = list(DURATION_PART_RE.finditer(raw))
    if not parts:
        return None
    # Reject stray non-separator text so an ambiguous phrase cannot be accepted
    # as a verified reset duration.
    remainder = DURATION_PART_RE.sub("", raw)
    if re.sub(r"[\s,]+|\band\b", "", remainder, flags=re.IGNORECASE):
        return None
    seconds = 0
    for part in parts:
        amount = int(part.group(1))
        unit = part.group(2).lower()
        if unit.startswith("day") or unit == "d":
            seconds += amount * 86400
        elif unit.startswith("hour") or unit == "h":
            seconds += amount * 3600
        elif unit.startswith("minute") or unit == "m":
            seconds += amount * 60
        else:
            seconds += amount
    return seconds if seconds > 0 else None


def _parse_refresh_deadline(text: str) -> datetime | None:
    """Return the latest verified reset plus guard, or None when unverified."""
    now = _utcnow()
    candidates: list[datetime] = []
    for match in REFRESH_TS_RE.finditer(text):
        raw = match.group("value").strip()
        duration_s = _parse_duration_seconds(raw)
        if duration_s is not None:
            candidates.append(now + timedelta(seconds=duration_s))
            continue
        try:
            normalized = raw.replace(" ", "T")
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            # Offsets without a colon are legal ISO input in Python; the regex
            # deliberately requires a timezone so local/naive text is rejected.
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                continue
            parsed = parsed.astimezone(timezone.utc)
            if parsed > now:
                candidates.append(parsed)
        except ValueError:
            continue
    if not candidates:
        return None
    return max(candidates) + timedelta(seconds=QUOTA_REFRESH_GUARD_SECONDS)


def _quota_hold_from_state(state: dict[str, Any] | None = None, *, now: float | None = None) -> tuple[bool, str]:
    state = state if state is not None else _load_json(SUPERVISOR_STATE_FILE, {})
    status = str(state.get("last_status") or "")
    if status == "quota_wait_unverified":
        return True, "quota reset is unverified; automatic AGY launches are disabled"
    if status == "quota_wait":
        deadline = float(state.get("next_run_at", 0) or 0)
        current = time.time() if now is None else float(now)
        if deadline > current:
            return True, f"quota guard active until {datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()}"
    return False, ""


class ConsoleInput(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class GoalConfig(BaseModel):
    enabled: bool = True
    goal: str | None = Field(default=None, max_length=12000)


class InteractiveConsole:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.master_fd: int | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.events: deque[dict[str, Any]] = deque(maxlen=2000)
        self.seq = 0
        self.started_at: float | None = None
        self.last_output_at: float | None = None
        self.lock = asyncio.Lock()

    def _append(self, text: str) -> None:
        cleaned = _clean_terminal(text)
        if not cleaned:
            return
        for line in cleaned.splitlines():
            self.seq += 1
            self.events.append({"seq": self.seq, "text": line[:1500], "ts": time.time()})
        self.last_output_at = time.time()

    async def start(self) -> None:
        async with self.lock:
            if self.process and self.process.returncode is None:
                return
            held, reason = _quota_hold_from_state()
            if held:
                raise RuntimeError(reason)
            _enforce_no_paid_credits()
            master_fd, slave_fd = pty.openpty()
            env = _agy_env()
            self.process = await asyncio.create_subprocess_exec(
                "agy",
                "--mode=accept-edits",
                cwd=str(WORKSPACE_DIR),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
            )
            os.close(slave_fd)
            self.master_fd = master_fd
            os.set_blocking(master_fd, False)
            self.started_at = time.time()
            self.last_output_at = self.started_at
            self._append("[Plane Alerts] AGY interactive console started")
            self.reader_task = asyncio.create_task(self._reader_loop(), name="agy-console-reader")

    async def _reader_loop(self) -> None:
        assert self.master_fd is not None
        while self.process and self.process.returncode is None:
            try:
                chunk = await asyncio.to_thread(os.read, self.master_fd, 8192)
                if not chunk:
                    await asyncio.sleep(0.1)
                    continue
                self._append(chunk.decode("utf-8", errors="replace"))
            except BlockingIOError:
                await asyncio.sleep(0.1)
            except OSError:
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("AGY console reader failed")
                await asyncio.sleep(0.5)
        if self.process:
            await self.process.wait()
            self._append(f"[Plane Alerts] AGY console exited with code {self.process.returncode}")

    async def send(self, text: str) -> None:
        if not self.process or self.process.returncode is not None or self.master_fd is None:
            raise RuntimeError("AGY console is not running")
        payload = (text.rstrip("\n") + "\n").encode("utf-8")
        await asyncio.to_thread(os.write, self.master_fd, payload)

    async def stop(self) -> None:
        async with self.lock:
            if not self.process or self.process.returncode is not None:
                return
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=8)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if self.reader_task and not self.reader_task.done():
                self.reader_task.cancel()
            if self.master_fd is not None:
                try:
                    os.close(self.master_fd)
                except OSError:
                    pass
            self.master_fd = None

    def output(self, cursor: int) -> dict[str, Any]:
        events = [event for event in self.events if int(event["seq"]) > cursor]
        running = bool(self.process and self.process.returncode is None)
        return {
            "running": running,
            "pid": self.process.pid if running and self.process else None,
            "cursor": self.seq,
            "events": events[-250:],
            "seconds_since_output": round(time.time() - self.last_output_at, 1) if self.last_output_at else None,
        }


console = InteractiveConsole()


class GoalSupervisor:
    def __init__(self) -> None:
        persisted = _load_json(SUPERVISOR_STATE_FILE, {})
        self.enabled = bool(persisted.get("enabled", False))
        self.goal = str(persisted.get("goal") or DEFAULT_GOAL)
        self.next_run_at = float(persisted.get("next_run_at", 0) or 0)
        self.last_run_at = float(persisted.get("last_run_at", 0) or 0)
        self.last_exit_code: int | None = persisted.get("last_exit_code")
        self.last_status = str(persisted.get("last_status") or "idle")
        self.last_output_at = float(persisted.get("last_output_at", 0) or 0)
        self.current_process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task[None] | None = None
        self.output_tail: deque[str] = deque(maxlen=300)
        self._restart_requested = False

    def _save(self) -> None:
        _atomic_json(
            SUPERVISOR_STATE_FILE,
            {
                **_load_json(SUPERVISOR_STATE_FILE, {}),
                "enabled": self.enabled,
                "goal": self.goal,
                "next_run_at": self.next_run_at,
                "last_run_at": self.last_run_at,
                "last_exit_code": self.last_exit_code,
                "last_status": self.last_status,
                "last_output_at": self.last_output_at,
            },
        )

    def quota_hold_active(self, *, now: float | None = None) -> tuple[bool, str]:
        return _quota_hold_from_state(
            {"last_status": self.last_status, "next_run_at": self.next_run_at},
            now=now,
        )

    async def start_task(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._loop(), name="agy-goal-supervisor")

    async def configure(self, enabled: bool, goal: str | None = None) -> None:
        self.enabled = enabled
        if goal:
            self.goal = goal
        held, _ = self.quota_hold_active()
        if enabled and self.next_run_at <= 0 and not held:
            self.next_run_at = time.time()
        self._save()
        await self.start_task()

    def _quota_limited(self, text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in QUOTA_PATTERNS)

    async def _run_goal(self) -> None:
        held, reason = self.quota_hold_active()
        if held:
            logger.warning("AGY launch blocked: %s", reason)
            self._save()
            return
        _enforce_no_paid_credits()
        self.last_run_at = time.time()
        self.last_output_at = self.last_run_at
        self.last_status = "running"
        self.output_tail.clear()
        self._save()

        command = [
            "agy",
            "-p",
            f"/goal {self.goal}",
            "--output-format",
            "stream-json",
            "--mode=accept-edits",
            "--print-timeout",
            "4h",
        ]
        env = _agy_env()
        self.current_process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(WORKSPACE_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        assert self.current_process.stdout is not None
        assert self.current_process.stderr is not None

        quota_text: list[str] = []

        async def consume(stream: asyncio.StreamReader, label: str) -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = _clean_terminal(line.decode("utf-8", errors="replace"))
                if not text:
                    continue
                self.last_output_at = time.time()
                rendered = f"[{label}] {text}"[:4000]
                self.output_tail.append(rendered)
                logger.info("AGY_GOAL %s", rendered)
                if self._quota_limited(text):
                    quota_text.append(text)
                self._save()

        stdout_task = asyncio.create_task(consume(self.current_process.stdout, "OUT"))
        stderr_task = asyncio.create_task(consume(self.current_process.stderr, "ERR"))

        while self.current_process.returncode is None:
            await asyncio.sleep(15)
            if time.time() - self.last_output_at > WATCHDOG_SILENCE_SECONDS:
                logger.warning(
                    "AGY goal produced no output for %ss; requesting a controlled restart",
                    WATCHDOG_SILENCE_SECONDS,
                )
                self._restart_requested = True
                try:
                    os.killpg(self.current_process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                break

        exit_code = await self.current_process.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        self.last_exit_code = exit_code
        self.current_process = None

        combined = "\n".join(list(self.output_tail)[-120:] + quota_text)
        if quota_text or self._quota_limited(combined):
            resume = _parse_refresh_deadline(combined)
            if resume is None:
                self.next_run_at = 0
                self.last_status = "quota_wait_unverified"
                logger.error(
                    "AGY baseline quota reached but no verified reset deadline was parsed; "
                    "automatic retries are disabled to prevent paid-credit usage"
                )
            else:
                self.next_run_at = resume.timestamp()
                self.last_status = "quota_wait"
                logger.warning("AGY baseline quota reached; paid credits disabled. Resume no earlier than %s", resume.isoformat())
        elif self._restart_requested:
            self._restart_requested = False
            self.next_run_at = time.time() + GOAL_RETRY_SECONDS
            self.last_status = "watchdog_restart"
        elif exit_code == 0:
            self.next_run_at = time.time() + GOAL_CYCLE_SECONDS
            self.last_status = "completed"
        else:
            self.next_run_at = time.time() + GOAL_RETRY_SECONDS
            self.last_status = "retry"
        self._save()

    async def _loop(self) -> None:
        while True:
            try:
                if not self.enabled:
                    await asyncio.sleep(5)
                    continue
                held, _ = self.quota_hold_active()
                if held:
                    await asyncio.sleep(30)
                    continue
                wait = self.next_run_at - time.time()
                if wait > 0:
                    await asyncio.sleep(min(wait, 30))
                    continue
                await self._run_goal()
            except asyncio.CancelledError:
                raise
            except FileNotFoundError:
                self.last_status = "agy_missing"
                self.next_run_at = time.time() + 300
                self._save()
                logger.exception("AGY binary missing")
                await asyncio.sleep(30)
            except Exception:
                # Never convert an active quota hold into a generic retry.
                held, _ = self.quota_hold_active()
                if not held:
                    self.last_status = "supervisor_error"
                    self.next_run_at = time.time() + GOAL_RETRY_SECONDS
                    self._save()
                logger.exception("AGY supervisor iteration failed")
                await asyncio.sleep(10)

    async def stop_current(self) -> None:
        if self.current_process and self.current_process.returncode is None:
            try:
                os.killpg(self.current_process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def status(self) -> dict[str, Any]:
        held, reason = self.quota_hold_active()
        return {
            "enabled": self.enabled,
            "status": self.last_status,
            "goal_running": bool(self.current_process and self.current_process.returncode is None),
            "last_run_at": self.last_run_at or None,
            "last_exit_code": self.last_exit_code,
            "next_run_at": self.next_run_at or None,
            "next_run_iso": datetime.fromtimestamp(self.next_run_at, tz=timezone.utc).isoformat() if self.next_run_at else None,
            "seconds_since_output": round(time.time() - self.last_output_at, 1) if self.last_output_at else None,
            "paid_credit_overages": False,
            "quota_hold_active": held,
            "quota_hold_reason": reason or None,
            "quota_deadline_verified": self.last_status != "quota_wait_unverified",
            "output_tail": list(self.output_tail)[-80:],
        }


supervisor = GoalSupervisor()


def _auth(x_agy_token: str | None = Header(default=None)) -> None:
    if not WORKER_TOKEN or x_agy_token != WORKER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


app = FastAPI(title="Plane Alerts AGY Prediction Lab", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    FINDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _enforce_no_paid_credits()
    await supervisor.start_task()
    logger.info("AGY worker ready; paid credit overages forced OFF; supervisor enabled=%s", supervisor.enabled)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "agy_binary": Path("/usr/local/bin/agy").exists(),
        "state_dir": str(STATE_DIR),
        "settings_present": SETTINGS_FILE.exists(),
        "paid_credit_overages": False,
        "console_running": bool(console.process and console.process.returncode is None),
        "supervisor": supervisor.status(),
    }


@app.post("/console/start", dependencies=[Depends(_auth)])
async def console_start() -> dict[str, Any]:
    try:
        await console.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return console.output(0)


@app.post("/console/input", dependencies=[Depends(_auth)])
async def console_input(payload: ConsoleInput) -> dict[str, Any]:
    await console.send(payload.text)
    return {"ok": True}


@app.post("/console/stop", dependencies=[Depends(_auth)])
async def console_stop() -> dict[str, Any]:
    await console.stop()
    return {"ok": True}


@app.get("/console/output", dependencies=[Depends(_auth)])
async def console_output(cursor: int = 0) -> dict[str, Any]:
    return console.output(max(0, cursor))


@app.post("/supervisor/config", dependencies=[Depends(_auth)])
async def supervisor_config(payload: GoalConfig) -> dict[str, Any]:
    await supervisor.configure(payload.enabled, payload.goal)
    return supervisor.status()


@app.post("/supervisor/stop-current", dependencies=[Depends(_auth)])
async def supervisor_stop_current() -> dict[str, Any]:
    await supervisor.stop_current()
    return supervisor.status()


@app.get("/supervisor/status", dependencies=[Depends(_auth)])
async def supervisor_status() -> dict[str, Any]:
    return supervisor.status()


@app.get("/findings", dependencies=[Depends(_auth)])
async def findings(after: int = 0, limit: int = 100) -> dict[str, Any]:
    if not FINDINGS_FILE.exists():
        return {"findings": [], "cursor": after}
    page_limit = max(1, min(limit, 500))
    # Keep only the earliest next page in memory. This remains O(limit) even
    # when the JSONL grows and is correct even if file order is imperfect.
    page: dict[int, dict[str, Any]] = {}
    with FINDINGS_FILE.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                seq = int(item.get("seq", 0) or 0)
            except (TypeError, ValueError):
                continue
            if seq <= after:
                continue
            if seq in page:
                page[seq] = item
                continue
            if len(page) < page_limit:
                page[seq] = item
                continue
            largest = max(page)
            if seq < largest:
                del page[largest]
                page[seq] = item
    ordered_seq = sorted(page)
    rows = [page[seq] for seq in ordered_seq]
    cursor = ordered_seq[-1] if ordered_seq else after
    return {"findings": rows, "cursor": cursor}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.agy_worker:app", host="0.0.0.0", port=PORT, reload=False)
