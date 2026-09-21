"""Same-conversation recovery for denied AGY headless tool choices.

Antigravity headless mode soft-denies non-allowlisted tool actions and still
returns a successful process exit. The CLI does not expose a host callback that
can rewrite a run_command before its internal permission check, so Plane Alerts
recovers at the conversation boundary instead: extract the conversation ID from
the streamed result, immediately resume that exact conversation, and inject
narrow corrective tooling guidance. Strict permissions remain unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Any, Iterable

from app import agy_worker as base

logger = logging.getLogger("plane_alerts.agy_tool_recovery_v431")

_ORIGINAL_RUN_GOAL = base.GoalSupervisor._run_goal
_ORIGINAL_STATUS = base.GoalSupervisor.status
_INSTALLED = False
_MAX_RECOVERY_TURNS = 4
_FALLBACK_RETRY_S = 15
_EXHAUSTED_RETRY_S = 30
_CONVERSATION_ID_RE = re.compile(r'"conversation_id"\s*:\s*"([^"\\]+)"')

_DENIAL_MARKERS = (
    "permission check failed",
    'required the "command" permission',
    '"denied_actions":[',
    '"denied_actions": [{',
)


@dataclass(slots=True)
class RecoveryTurn:
    exit_code: int
    denied: bool
    quota_limited: bool
    watchdog_restart: bool
    conversation_id: str | None
    combined_output: str


def output_has_permission_denial(lines: Iterable[str]) -> bool:
    """Return True when AGY output contains a real headless permission denial."""
    for line in lines:
        lowered = str(line).lower()
        if any(marker in lowered for marker in _DENIAL_MARKERS):
            return True
    return False


def _json_payload(line: str) -> dict[str, Any] | None:
    """Extract one complete stream-json object from a supervisor-rendered line."""
    text = str(line).strip()
    brace = text.find("{")
    if brace < 0:
        return None
    try:
        payload = json.loads(text[brace:])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def extract_conversation_id(lines: Iterable[str]) -> str | None:
    """Recover the latest conversation ID, including from a truncated JSON line."""
    materialized = list(lines)
    for line in reversed(materialized):
        raw = str(line)
        payload = _json_payload(raw)
        if payload:
            candidates: list[Any] = [payload.get("conversation_id")]
            for key in ("result", "step_update", "init"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested.get("conversation_id"))
            for candidate in candidates:
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()

        match = _CONVERSATION_ID_RE.search(raw)
        if match:
            return match.group(1).strip()
    return None


def recovery_prompt(attempt: int) -> str:
    """Escalate from a reminder to a Python-only inspection path."""
    if attempt <= 1:
        return (
            "The previous headless run_command was soft-denied by the strict Plane Alerts permission policy. "
            "This is recoverable and is NOT task completion. Continue the same audit exactly where you stopped. "
            "Do not retry the denied command or an equivalent shell workaround. For inspection use view_file, "
            "list_dir, or grep_search. For multi-step analysis use write_to_file to create a Python standard-library "
            "script, then run exactly one command: python3 /path/to/script.py. Standalone git, pytest, grep, ls, "
            "python, python3, and agy_record_finding commands are allowed when genuinely needed. Never use cat, "
            "find, sed, head, tail, jq, pipes, redirection, &&, ||, semicolon command chains, heredocs, sh, or bash. "
            "Continue until the original Plane Alerts goal is genuinely complete."
        )
    if attempt == 2:
        return (
            "TOOLING RECOVERY MODE: this conversation has repeated an unsupported shell-style tool choice. "
            "Continue the original audit without restarting or summarizing it. Do not use run_command for file "
            "inspection, filtering, slicing, searching, or file creation. Use view_file/list_dir/grep_search instead. "
            "If custom analysis is required, write a Python standard-library script with write_to_file and execute "
            "only that script with one python3 command. git and pytest may be standalone commands only. Do not use "
            "cat/find/sed/head/tail/jq, pipes, redirects, command chaining, heredocs, sh, or bash."
        )
    return (
        "FINAL TOOLING RECOVERY MODE: stop using run_command for inspection for the remainder of this goal. "
        "Use only view_file, list_dir, grep_search, write_to_file, and a single python3 script invocation for custom "
        "analysis. The only other run_command use permitted for this goal is the dedicated agy_record_finding.py "
        "recorder, or standalone git/pytest if the original goal truly requires them. Never use cat, find, sed, "
        "head, tail, jq, pipes, redirects, heredocs, sh, bash, or compound shell syntax. Continue the same audit "
        "from the exact point where the denied action occurred."
    )


async def _run_resume_turn(self: Any, conversation_id: str, attempt: int) -> RecoveryTurn:
    held, reason = self.quota_hold_active()
    if held:
        raise RuntimeError(f"AGY tooling recovery launch blocked: {reason}")

    prompt = recovery_prompt(attempt)
    command = [
        "agy",
        "-p",
        prompt,
        "--conversation",
        conversation_id,
        "--output-format",
        "stream-json",
        "--mode=accept-edits",
        "--print-timeout",
        "4h",
    ]
    env = base._agy_env()
    self.last_status = "tool_recovery"
    self.last_output_at = time.time()
    self._save()

    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(base.WORKSPACE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,
    )
    self.current_process = process
    assert process.stdout is not None
    assert process.stderr is not None

    turn_lines: list[str] = []
    quota_lines: list[str] = []

    async def consume(stream: asyncio.StreamReader, label: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = base._clean_terminal(line.decode("utf-8", errors="replace"))
            if not text:
                continue
            self.last_output_at = time.time()
            rendered = f"[{label}] {text}"[:4000]
            turn_lines.append(rendered)
            self.output_tail.append(rendered)
            logger.info("AGY_RECOVERY %s", rendered)
            if self._quota_limited(text):
                quota_lines.append(text)
            self._save()

    stdout_task = asyncio.create_task(consume(process.stdout, "OUT"))
    stderr_task = asyncio.create_task(consume(process.stderr, "ERR"))

    watchdog_restart = False
    while process.returncode is None:
        await asyncio.sleep(15)
        if time.time() - self.last_output_at > base.WATCHDOG_SILENCE_SECONDS:
            watchdog_restart = True
            logger.warning(
                "AGY recovery conversation produced no output for %ss; restarting through normal supervisor path",
                base.WATCHDOG_SILENCE_SECONDS,
            )
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            break

    exit_code = await process.wait()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    self.last_exit_code = exit_code
    self.current_process = None

    combined = "\n".join(turn_lines + quota_lines)
    recovered_conversation_id = extract_conversation_id(turn_lines) or conversation_id
    return RecoveryTurn(
        exit_code=exit_code,
        denied=output_has_permission_denial(turn_lines),
        quota_limited=bool(quota_lines) or self._quota_limited(combined),
        watchdog_restart=watchdog_restart,
        conversation_id=recovered_conversation_id,
        combined_output=combined,
    )


async def _run_goal_with_same_conversation_recovery(self: Any) -> None:
    # The durable quota hold dominates the recovery wrapper itself. In
    # particular, an unverified reset must not fall through to stale denied-tool
    # output and accidentally resume a paid-credit-eligible conversation.
    held, reason = self.quota_hold_active()
    if held:
        self._tool_recovery_count_v431 = 0
        logger.warning("AGY tooling recovery blocked: %s", reason)
        return

    await _ORIGINAL_RUN_GOAL(self)

    held, reason = self.quota_hold_active()
    if held:
        self._tool_recovery_count_v431 = 0
        logger.warning("AGY tooling recovery remains blocked after goal turn: %s", reason)
        return

    initial_lines = tuple(self.output_tail)
    if not output_has_permission_denial(initial_lines):
        self._tool_recovery_count_v431 = 0
        return

    conversation_id = extract_conversation_id(initial_lines)
    if not conversation_id:
        self.last_status = "permission_retry_no_conversation"
        self.next_run_at = time.time() + _FALLBACK_RETRY_S
        self._save()
        logger.warning(
            "AGY tool denial had no recoverable conversation ID; using short %ss retry without widening permissions",
            _FALLBACK_RETRY_S,
        )
        return

    for attempt in range(1, _MAX_RECOVERY_TURNS + 1):
        held, reason = self.quota_hold_active()
        if held:
            self._tool_recovery_count_v431 = 0
            logger.warning("AGY tooling recovery blocked before resume turn: %s", reason)
            return
        self._tool_recovery_count_v431 = attempt
        logger.warning(
            "AGY headless tool choice denied; immediately resuming conversation=%s recovery_turn=%d/%d",
            conversation_id,
            attempt,
            _MAX_RECOVERY_TURNS,
        )
        turn = await _run_resume_turn(self, conversation_id, attempt)
        conversation_id = turn.conversation_id or conversation_id

        if turn.quota_limited:
            resume = base._parse_refresh_deadline(turn.combined_output)
            if resume is None:
                self.next_run_at = 0
                self.last_status = "quota_wait_unverified"
                self._save()
                logger.error(
                    "AGY baseline quota reached during tooling recovery with no verified reset deadline; "
                    "automatic retries disabled"
                )
            else:
                self.next_run_at = resume.timestamp()
                self.last_status = "quota_wait"
                self._save()
                logger.warning(
                    "AGY baseline quota reached during tooling recovery; paid credits remain disabled. Resume no earlier than %s",
                    resume.isoformat(),
                )
            return

        if turn.watchdog_restart:
            self.next_run_at = time.time() + base.GOAL_RETRY_SECONDS
            self.last_status = "watchdog_restart"
            self._save()
            return

        if turn.exit_code != 0:
            self.next_run_at = time.time() + base.GOAL_RETRY_SECONDS
            self.last_status = "retry"
            self._save()
            return

        if not turn.denied:
            self._tool_recovery_count_v431 = 0
            self.next_run_at = time.time() + base.GOAL_CYCLE_SECONDS
            self.last_status = "completed_after_tool_recovery"
            self._save()
            logger.info(
                "AGY recovered from denied tool choice inside the same conversation and completed normally conversation=%s",
                conversation_id,
            )
            return

    self.last_status = "tool_recovery_exhausted"
    self.next_run_at = time.time() + _EXHAUSTED_RETRY_S
    self._save()
    logger.warning(
        "AGY exhausted %d same-conversation tooling corrections; scheduling short %ss retry with strict permissions",
        _MAX_RECOVERY_TURNS,
        _EXHAUSTED_RETRY_S,
    )


def _status_with_tool_recovery(self: Any) -> dict[str, Any]:
    status = _ORIGINAL_STATUS(self)
    status["tool_recovery_mode"] = "same_conversation"
    status["tool_recovery_count"] = int(getattr(self, "_tool_recovery_count_v431", 0) or 0)
    status["tool_recovery_max_turns"] = _MAX_RECOVERY_TURNS
    return status


def install_tool_recovery_v431() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    base.GoalSupervisor._run_goal = _run_goal_with_same_conversation_recovery
    base.GoalSupervisor.status = _status_with_tool_recovery
    _INSTALLED = True
    logger.info("AGY v4.3.1 same-conversation tooling recovery enabled")
