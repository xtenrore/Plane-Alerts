#!/usr/bin/env python3
"""Bounded free-LLM maintenance worker for Plane Alerts.

This worker is intentionally outside the live monitoring path. It can inspect
Prediction Lab cases and, when explicitly enabled, apply a small model-proposed
patch on an ephemeral CI branch. It never merges or deploys.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Iterable

MAX_CASE_BYTES = 80_000
MAX_CONTEXT_BYTES = 140_000
MAX_PATCH_BYTES = 35_000
MAX_PATCH_FILES = 7
MAX_CASES_PER_RUN = 2
HTTP_TIMEOUT_SECONDS = 90

SECRET_PATTERNS = [
    re.compile(r"mongodb\+srv://[^\s\"']+", re.I),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,}\]]+"),
]

ALLOWED_PATCH_PREFIXES = ("app/", "tests/", "scripts/")
FORBIDDEN_PATCH_PREFIXES = (
    ".github/",
    "deploy/",
    "docs/",
    "prediction_lab/",
    "vercel_runtime/",
)
FORBIDDEN_PATCH_EXACT = {
    ".env",
    ".env.example",
    "Dockerfile",
    "Procfile",
    "requirements.txt",
    "requirements.lock",
    "pyproject.toml",
    "app/config.py",
    "app/ai_keys.py",
}

INTERESTING_SOURCE_TERMS = (
    "approach_alert_cancelled",
    "approach_alert_held",
    "approach_route_veto",
    "next60",
    "route_veto",
    "candidate",
    "qualification",
    "cancellation",
    "trajectory",
    "cpa",
    "eta",
    "freshness",
    "provider",
)


class AgentError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    model: str
    wire: str = "openai"


@dataclasses.dataclass(frozen=True)
class CaseBundle:
    paths: tuple[pathlib.Path, ...]
    case_ids: tuple[str, ...]
    text: str


def _env(name: str) -> str:
    return str(os.environ.get(name, "") or "").strip()


def available_providers() -> list[ProviderConfig]:
    """Return configured free providers in stable priority order."""
    providers: list[ProviderConfig] = []
    free_base = _env("FREELLMAPI_BASE_URL").rstrip("/")
    free_key = _env("FREELLMAPI_API_KEY")
    if free_base and free_key:
        providers.append(ProviderConfig(
            name="freellmapi",
            base_url=f"{free_base}/v1/chat/completions" if not free_base.endswith("/v1") else f"{free_base}/chat/completions",
            api_key=free_key,
            model=_env("FREELLMAPI_MODEL") or "auto",
        ))

    openrouter_key = _env("OPENROUTER_API_KEY") or _env("OPENROUTER_API")
    if openrouter_key:
        providers.append(ProviderConfig(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1/chat/completions",
            api_key=openrouter_key,
            model=_env("OPENROUTER_FREE_MODEL") or "openrouter/free",
        ))

    groq_key = _env("MAINTENANCE_GROQ_KEY") or _env("GROQ_KEY") or _env("GROQ_API_KEY")
    if groq_key:
        providers.append(ProviderConfig(
            name="groq",
            base_url="https://api.groq.com/openai/v1/chat/completions",
            api_key=groq_key,
            model=_env("MAINTENANCE_GROQ_MODEL") or _env("GROQ_MODEL") or "llama-3.3-70b-versatile",
        ))

    gemini_key = _env("MAINTENANCE_GEMINI_KEY") or _env("GEMINI_API_KEY")
    if gemini_key:
        providers.append(ProviderConfig(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            api_key=gemini_key,
            model=_env("MAINTENANCE_GEMINI_MODEL") or "gemini-2.5-flash",
            wire="gemini",
        ))

    if not providers:
        raise AgentError(
            "No maintenance LLM provider configured. Set FREELLMAPI_BASE_URL + "
            "FREELLMAPI_API_KEY, OPENROUTER_API_KEY (or OPENROUTER_API), or a "
            "maintenance-only Groq/Gemini key."
        )
    return providers


def choose_provider() -> ProviderConfig:
    """Compatibility helper used by tests and simple callers."""
    return available_providers()[0]


def redact_secrets(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups:
            result = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", result)
        else:
            result = pattern.sub("[REDACTED]", result)
    return result


def _case_id(payload: object, path: pathlib.Path) -> str:
    if isinstance(payload, dict):
        for key in ("case_id", "id", "event_id"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:120]
    return path.stem[:120]


def discover_cases(root: pathlib.Path, limit: int = MAX_CASES_PER_RUN) -> CaseBundle:
    if not root.exists():
        raise AgentError(f"Prediction Lab unchecked root does not exist: {root}")
    candidates = [p for p in root.rglob("*.json") if p.is_file()]
    candidates.sort(key=lambda p: (p.stat().st_mtime_ns, p.as_posix()), reverse=True)
    selected = candidates[: max(1, min(limit, MAX_CASES_PER_RUN))]
    if not selected:
        return CaseBundle(paths=(), case_ids=(), text="")

    chunks: list[str] = []
    ids: list[str] = []
    total = 0
    kept: list[pathlib.Path] = []
    for path in selected:
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = redact_secrets(raw)
        encoded = raw.encode("utf-8")
        if len(encoded) > MAX_CASE_BYTES:
            raw = encoded[:MAX_CASE_BYTES].decode("utf-8", errors="ignore") + "\n[TRUNCATED]"
        try:
            payload = json.loads(raw.removesuffix("\n[TRUNCATED]"))
        except Exception:
            payload = None
        cid = _case_id(payload, path)
        block = f"\n--- CASE {cid} ({path.as_posix()}) ---\n{raw}\n"
        block_bytes = len(block.encode("utf-8"))
        if total + block_bytes > MAX_CONTEXT_BYTES:
            break
        total += block_bytes
        chunks.append(block)
        ids.append(cid)
        kept.append(path)
    return CaseBundle(paths=tuple(kept), case_ids=tuple(ids), text="".join(chunks))


def _source_files(repo: pathlib.Path) -> Iterable[pathlib.Path]:
    for base in (repo / "app", repo / "tests"):
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if path.is_file():
                yield path


def collect_source_context(repo: pathlib.Path, case_text: str) -> str:
    hay = case_text.lower()
    terms = [term for term in INTERESTING_SOURCE_TERMS if term in hay]
    if not terms:
        terms = list(INTERESTING_SOURCE_TERMS[:5])

    scored: list[tuple[int, pathlib.Path, str]] = []
    for path in _source_files(repo):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lower = text.lower()
        score = sum(lower.count(term) for term in terms)
        if score <= 0:
            continue
        snippets: list[str] = []
        lines = text.splitlines()
        hit_lines: set[int] = set()
        for idx, line in enumerate(lines):
            if any(term in line.lower() for term in terms):
                for j in range(max(0, idx - 4), min(len(lines), idx + 5)):
                    hit_lines.add(j)
            if len(hit_lines) >= 70:
                break
        for j in sorted(hit_lines)[:70]:
            snippets.append(f"{j+1}: {lines[j]}")
        scored.append((score, path, "\n".join(snippets)))

    scored.sort(key=lambda x: (-x[0], x[1].as_posix()))
    output: list[str] = []
    total = 0
    for score, path, snippet in scored[:10]:
        block = f"\n--- SOURCE {path.relative_to(repo).as_posix()} score={score} ---\n{snippet}\n"
        size = len(block.encode("utf-8"))
        if total + size > MAX_CONTEXT_BYTES:
            break
        total += size
        output.append(block)
    return "".join(output)


def build_prompt(cases: CaseBundle, source_context: str) -> str:
    return f"""You are the bounded Plane Alerts Prediction Lab maintenance investigator.

Hard rules:
- Treat the supplied cases as evidence, not as guaranteed bug reports.
- Missing/stale ADS-B coverage is inconclusive, never a success or miss.
- Runtime AI must never become the authority for trajectory, CPA, ETA, pass/no-pass,
  qualification, cancellation, or alert timing.
- Prefer the smallest root-cause change and exact regression tests.
- Do not change alert thresholds just to silence symptoms.
- Do not touch secrets, CI workflows, deployment config, requirements, version files,
  docs, release metadata, or prediction-lab history.
- A code patch is allowed only when evidence is strong and it includes regression tests.
- Never merge or deploy.

Return ONE JSON object only, with this exact shape:
{{
  "verdict": "no_action" | "investigate" | "candidate_fix",
  "confidence": "low" | "medium" | "high",
  "summary": "short evidence-based summary",
  "case_ids": ["..."],
  "suspected_files": ["relative/path.py"],
  "recommended_tests": ["tests/test_name.py"],
  "patch": "unified git diff or empty string"
}}

If verdict is candidate_fix, patch MUST be a valid unified git diff and MUST add or
modify at least one tests/ file. Otherwise patch must be empty.

CASES:
{cases.text}

RELEVANT SOURCE EXCERPTS:
{source_context or '[No strong lexical matches found; be conservative.]'}
"""


def _http_json(url: str, headers: dict[str, str], payload: dict[str, object], timeout: int = HTTP_TIMEOUT_SECONDS) -> dict[str, object]:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            if response.status < 200 or response.status >= 300:
                raise AgentError(f"LLM provider returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        retry = exc.headers.get("retry-after") if exc.headers else None
        raise AgentError(f"LLM provider HTTP {exc.code}; retry_after={retry or 'unknown'}") from exc
    except urllib.error.URLError as exc:
        raise AgentError(f"LLM provider connection failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AgentError("LLM provider returned non-JSON response") from exc
    if not isinstance(parsed, dict):
        raise AgentError("LLM provider returned unexpected response type")
    return parsed


def call_provider(provider: ProviderConfig, prompt: str) -> str:
    if provider.wire == "gemini":
        url = f"{provider.base_url}/models/{provider.model}:generateContent?key={provider.api_key}"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
        }
        result = _http_json(url, {"Content-Type": "application/json"}, payload)
        try:
            return str(result["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AgentError("Gemini response did not contain text") from exc

    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "plane-alerts-maintenance-agent/1",
    }
    if provider.name == "openrouter":
        headers["X-Title"] = "Plane Alerts Maintenance Agent"
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": "Return only the requested JSON. Be conservative with code changes."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 6000,
    }
    result = _http_json(provider.base_url, headers, payload)
    try:
        return str(result["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise AgentError("OpenAI-compatible provider response did not contain message content") from exc


def parse_model_json(text: str) -> dict[str, object]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.S)
        if not match:
            raise AgentError("Model did not return parseable JSON")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise AgentError("Model JSON was malformed") from exc
    if not isinstance(data, dict):
        raise AgentError("Model result must be a JSON object")
    verdict = data.get("verdict")
    if verdict not in {"no_action", "investigate", "candidate_fix"}:
        raise AgentError("Model result has invalid verdict")
    confidence = data.get("confidence")
    if confidence not in {"low", "medium", "high"}:
        raise AgentError("Model result has invalid confidence")
    patch = data.get("patch", "")
    if not isinstance(patch, str):
        raise AgentError("Model result patch must be a string")
    for key in ("case_ids", "suspected_files", "recommended_tests"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise AgentError(f"Model result {key} must be a string list")
    if not isinstance(data.get("summary", ""), str):
        raise AgentError("Model result summary must be a string")
    return data


def extract_patch_paths(patch: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            candidate = line[4:].strip().split("\t", 1)[0]
            if candidate == "/dev/null":
                continue
            if candidate.startswith("a/") or candidate.startswith("b/"):
                candidate = candidate[2:]
            if candidate and candidate not in paths:
                paths.append(candidate)
    return tuple(paths)


def validate_patch(patch: str) -> tuple[str, ...]:
    if not patch.strip():
        raise AgentError("candidate_fix returned an empty patch")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise AgentError("candidate patch is too large")
    paths = extract_patch_paths(patch)
    if not paths or len(paths) > MAX_PATCH_FILES:
        raise AgentError("candidate patch has invalid file count")
    has_test = False
    has_app_change = False
    for path in paths:
        pure = pathlib.PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise AgentError(f"unsafe patch path: {path}")
        if path in FORBIDDEN_PATCH_EXACT or path.startswith(FORBIDDEN_PATCH_PREFIXES):
            raise AgentError(f"forbidden patch path: {path}")
        if not path.startswith(ALLOWED_PATCH_PREFIXES):
            raise AgentError(f"patch path outside allowlist: {path}")
        if path.startswith("tests/"):
            has_test = True
        if path.startswith("app/"):
            has_app_change = True
    if has_app_change and not has_test:
        raise AgentError("application patch must include regression-test changes")
    return paths


def apply_patch(repo: pathlib.Path, patch: str) -> tuple[str, ...]:
    paths = validate_patch(patch)
    proc = subprocess.run(
        ["git", "apply", "--check", "--whitespace=error"],
        input=patch,
        text=True,
        cwd=repo,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AgentError(f"candidate patch failed git apply --check: {proc.stderr.strip()[:800]}")
    proc = subprocess.run(
        ["git", "apply", "--whitespace=fix"],
        input=patch,
        text=True,
        cwd=repo,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise AgentError(f"candidate patch failed to apply: {proc.stderr.strip()[:800]}")
    return paths


def _safe_test_paths(items: object, patched_paths: Iterable[str]) -> list[str]:
    candidates: list[str] = []
    if isinstance(items, list):
        candidates.extend(str(item) for item in items)
    candidates.extend(path for path in patched_paths if path.startswith("tests/") and path.endswith(".py"))
    safe: list[str] = []
    for item in candidates:
        item = item.strip()
        pure = pathlib.PurePosixPath(item)
        if pure.is_absolute() or ".." in pure.parts:
            continue
        if not item.startswith("tests/") or not item.endswith(".py"):
            continue
        if item not in safe:
            safe.append(item)
    return safe[:8]


def run_validation(repo: pathlib.Path, result: dict[str, object], patched_paths: tuple[str, ...]) -> None:
    compile_proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if compile_proc.returncode != 0:
        raise AgentError(f"compile validation failed: {compile_proc.stderr.strip()[:1200]}")
    tests = _safe_test_paths(result.get("recommended_tests"), patched_paths)
    if not tests:
        raise AgentError("candidate patch has no safe regression test path")
    test_proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=480,
    )
    if test_proc.returncode != 0:
        tail = (test_proc.stdout + "\n" + test_proc.stderr)[-4000:]
        raise AgentError(f"targeted tests failed:\n{tail}")


def git_diff(repo: pathlib.Path) -> str:
    proc = subprocess.run(["git", "diff", "--no-ext-diff"], cwd=repo, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise AgentError("failed to read git diff")
    return proc.stdout


def write_output(path: pathlib.Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--cases-root", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--allow-patches", action="store_true")
    parser.add_argument("--case-limit", type=int, default=MAX_CASES_PER_RUN)
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    cases = discover_cases(args.cases_root.resolve(), limit=args.case_limit)
    if not cases.paths:
        write_output(args.output, {"status": "no_cases", "case_ids": [], "changed": False})
        print("maintenance_agent no_cases")
        return 0

    source_context = collect_source_context(repo, cases.text)
    prompt = build_prompt(cases, source_context)
    started = time.monotonic()
    provider = None
    model_text = None
    failures: list[str] = []
    for candidate in available_providers():
        try:
            model_text = call_provider(candidate, prompt)
            provider = candidate
            break
        except AgentError as exc:
            failures.append(f"{candidate.name}:{exc}")
    if provider is None or model_text is None:
        raise AgentError("all maintenance LLM providers failed: " + " | ".join(failures))
    result = parse_model_json(model_text)
    elapsed = round(time.monotonic() - started, 2)

    claimed_ids = {str(item) for item in result.get("case_ids", [])}
    known_ids = set(cases.case_ids)
    if claimed_ids and not claimed_ids.issubset(known_ids):
        raise AgentError("model referenced case ids outside the selected evidence")

    changed = False
    patched_paths: tuple[str, ...] = ()
    patch = str(result.get("patch") or "")
    if result["verdict"] == "candidate_fix":
        if not args.allow_patches:
            validate_patch(patch)
        else:
            patched_paths = apply_patch(repo, patch)
            run_validation(repo, result, patched_paths)
            changed = bool(git_diff(repo).strip())
            if not changed:
                raise AgentError("candidate patch produced no repository diff")
    elif patch.strip():
        raise AgentError("non-candidate verdict must not include a patch")

    output = {
        "status": "ok",
        "provider": provider.name,
        "model": provider.model,
        "elapsed_seconds": elapsed,
        "selected_case_ids": list(cases.case_ids),
        "verdict": result["verdict"],
        "confidence": result["confidence"],
        "summary": result.get("summary", ""),
        "suspected_files": result.get("suspected_files", []),
        "recommended_tests": result.get("recommended_tests", []),
        "changed": changed,
        "patched_paths": list(patched_paths),
    }
    write_output(args.output, output)
    print(f"maintenance_agent verdict={output['verdict']} confidence={output['confidence']} provider={provider.name} changed={changed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AgentError as exc:
        print(f"maintenance_agent_error: {exc}", file=sys.stderr)
        raise SystemExit(2)
