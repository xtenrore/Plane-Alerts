"""Disabled-by-default, free-only provider adapters for AI *analysis* tasks.

No adapter can invoke Plane Alerts flight decision code. Actual invocation
requires an explicit model allowlist and a separate runtime feature flag.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Mapping, Protocol

PROVIDERS = ("groq", "mistral", "gemini", "cloudflare", "openrouter")
SLOT_NAMES = {
    "groq": tuple(["GROQ_KEY"] + [f"GROQ_KEY_{i}" for i in range(2, 6)]),
    "mistral": tuple(["MISTRAL_API"] + [f"MISTRAL_API_{i}" for i in range(2, 6)]),
    "gemini": tuple(["GEMINI_API_KEY"] + [f"GEMINI_API_KEY_{i}" for i in range(2, 6)]),
    "cloudflare": ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_2"),
    "openrouter": ("OPENROUTER_API",),
}
ACCOUNT_NAMES = ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID_2")


@dataclass(frozen=True)
class Slot:
    provider: str
    name: str
    credential: str
    account_id: str = ""

    def __repr__(self) -> str:
        return f"Slot(provider={self.provider!r},name={self.name!r},credential=<redacted>)"


@dataclass(frozen=True)
class AnalysisTask:
    purpose: str
    prompt: str

    def __post_init__(self) -> None:
        if self.purpose not in ("triage", "independent_review", "deep_investigation", "supervisor"):
            raise ValueError("only analysis/control-plane tasks are permitted")
        if not self.prompt or len(self.prompt.encode()) > 16384:
            raise ValueError("bounded analysis prompt required")


@dataclass(frozen=True)
class AnalysisResult:
    provider: str
    slot_name: str
    model: str
    analysis: dict
    input_tokens: int
    output_tokens: int


class ProviderFailure(Exception):
    def __init__(self, kind: str, *, status: int = 0, retry_after: int = 0, provider_wide: bool = False):
        super().__init__(kind)  # Never include request, body, URL, or credentials.
        self.kind, self.status, self.retry_after, self.provider_wide = kind, status, retry_after, provider_wide


class HTTPTransport(Protocol):
    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, Mapping[str, str], bytes]: ...


class UrllibTransport:
    def post(self, url: str, headers: Mapping[str, str], body: bytes) -> tuple[int, Mapping[str, str], bytes]:
        req = urllib.request.Request(url, data=body, method="POST", headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                return response.status, dict(response.headers), response.read(131073)
        except urllib.error.HTTPError as exc:
            # Error response text is untrusted and may reflect credentials.
            return exc.code, dict(exc.headers), b""
        except Exception:
            raise ProviderFailure("network") from None

    def get_status(self, url: str, headers: Mapping[str, str]) -> int:
        """Fetch metadata status only; never log or retain provider response."""
        req = urllib.request.Request(url, method="GET", headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:
            raise ProviderFailure("network") from None


def retry_after(headers: Mapping[str, str], *, now: float) -> int:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), "")
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        try:
            seconds = int(parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError, IndexError):
            seconds = 60
    return min(3600, max(1, seconds))


def configured_slots(env: Mapping[str, str]) -> list[Slot]:
    slots = []
    for provider, names in SLOT_NAMES.items():
        for i, name in enumerate(names):
            credential = env.get(name, "")
            if not credential:
                continue
            account = env.get(ACCOUNT_NAMES[i], "") if provider == "cloudflare" else ""
            if provider == "cloudflare" and not re.fullmatch(r"[0-9a-fA-F]{32}", account):
                # Never cross-pair an account and token from another slot.
                continue
            slots.append(Slot(provider, name, credential, account))
    return slots


def schema_test_mode(provider: str, model: str, response: bytes) -> dict:
    """Validate a synthetic provider envelope without credentials or network use."""
    if provider not in PROVIDERS or not re.fullmatch(r"[A-Za-z0-9@._/:-]{1,100}", model):
        raise ValueError("invalid analysis capability")
    if len(response) > 131072:
        raise ProviderFailure("malformed")
    try:
        envelope = json.loads(response)
        if provider == "gemini":
            content = envelope["candidates"][0]["content"]["parts"][0]["text"]
        elif provider == "cloudflare":
            content = envelope["result"]["response"]
        else:
            content = envelope["choices"][0]["message"]["content"]
        return _response_json(content)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ProviderFailure("malformed") from None


def _response_json(content: str) -> dict:
    if len(content.encode()) > 65536:
        raise ProviderFailure("malformed")
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        raise ProviderFailure("malformed") from None
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str) or not isinstance(value.get("findings"), list):
        raise ProviderFailure("schema")
    if len(value["summary"]) > 4000 or len(value["findings"]) > 64:
        raise ProviderFailure("schema")
    return value


class Adapter:
    def __init__(self, transport: HTTPTransport | None = None):
        self.transport = transport or UrllibTransport()

    def probe(self, slot: Slot) -> str:
        """Read provider model metadata without running inference or spending tokens."""
        if slot.provider not in PROVIDERS or slot.name not in SLOT_NAMES[slot.provider] or not slot.credential:
            raise ValueError("unconfigured provider slot")
        if slot.provider == "cloudflare" and not re.fullmatch(r"[0-9a-fA-F]{32}", slot.account_id):
            raise ValueError("paired Cloudflare account required")
        urls = {"groq": "https://api.groq.com/openai/v1/models",
                "mistral": "https://api.mistral.ai/v1/models",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
                "openrouter": "https://openrouter.ai/api/v1/models",
                "cloudflare": f"https://api.cloudflare.com/client/v4/accounts/{slot.account_id}/ai/models/search"}
        headers = ({"x-goog-api-key": slot.credential} if slot.provider == "gemini"
                   else {"Authorization": "Bearer " + slot.credential})
        # Test transports can implement the metadata operation without network.
        status = self.transport.get_status(urls[slot.provider], headers)
        if status == 401:
            return "unauthorized"
        if status == 403:
            return "forbidden"
        if status == 429:
            return "limited"
        return "available" if status == 200 else "unavailable"

    def execute(self, slot: Slot, task: AnalysisTask, model: str, *, now: float) -> AnalysisResult:
        if slot.provider not in PROVIDERS or slot.name not in SLOT_NAMES[slot.provider] or not slot.credential:
            raise ValueError("unconfigured provider slot")
        if not re.fullmatch(r"[A-Za-z0-9@._/:-]{1,100}", model):
            raise ValueError("invalid model identifier")
        instruction = "Return JSON with summary and findings for independent audit only. Never decide aircraft trajectory, CPA, ETA, pass/no-pass, alert qualification, cancellation, or timing."
        headers = {"Content-Type": "application/json"}
        if slot.provider == "gemini":
            url = "https://generativelanguage.googleapis.com/v1beta/models/" + urllib.parse.quote(model, safe="") + ":generateContent"
            headers["x-goog-api-key"] = slot.credential
            payload = {"contents": [{"parts": [{"text": instruction + "\n" + task.prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "maxOutputTokens": 1024}}
        elif slot.provider == "cloudflare":
            if not re.fullmatch(r"[0-9a-fA-F]{32}", slot.account_id) or not model.startswith("@cf/"):
                raise ValueError("paired Cloudflare account and catalog model required")
            url = f"https://api.cloudflare.com/client/v4/accounts/{slot.account_id}/ai/run/{model}"
            headers["Authorization"] = "Bearer " + slot.credential
            payload = {"messages": [{"role": "system", "content": instruction}, {"role": "user", "content": task.prompt}], "max_tokens": 1024}
        else:
            url = {"groq": "https://api.groq.com/openai/v1/chat/completions",
                   "mistral": "https://api.mistral.ai/v1/chat/completions",
                   "openrouter": "https://openrouter.ai/api/v1/chat/completions"}[slot.provider]
            if slot.provider == "openrouter" and not (model.endswith(":free") or model == "openrouter/free"):
                raise ValueError("paid OpenRouter route refused")
            headers["Authorization"] = "Bearer " + slot.credential
            payload = {"model": model, "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": task.prompt}], "max_tokens": 1024}
            if slot.provider == "mistral":
                payload["response_format"] = {"type": "json_object"}
        status, response_headers, raw = self.transport.post(url, headers, json.dumps(payload, separators=(",", ":")).encode())
        if status != 200:
            scope = next((str(v).lower() for k, v in response_headers.items() if k.lower() == "x-ratelimit-scope"), "")
            kind = "quota" if status == 429 else "auth" if status in (401, 403) else "server" if status >= 500 else "request"
            raise ProviderFailure(kind, status=status, retry_after=retry_after(response_headers, now=now) if status == 429 else 0,
                                  provider_wide=scope in ("organization", "account", "provider"))
        if len(raw) > 131072:
            raise ProviderFailure("malformed")
        try:
            envelope = json.loads(raw)
            if slot.provider == "gemini":
                content = envelope["candidates"][0]["content"]["parts"][0]["text"]
                usage = envelope.get("usageMetadata", {})
                incoming, outgoing = usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)
            elif slot.provider == "cloudflare":
                content = envelope["result"]["response"]
                incoming, outgoing = 0, 0
            else:
                content = envelope["choices"][0]["message"]["content"]
                usage = envelope.get("usage", {})
                incoming, outgoing = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
            analysis = _response_json(content)
            if any(not isinstance(v, int) or v < 0 or v > 100000 for v in (incoming, outgoing)):
                raise ValueError("invalid usage")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderFailure("malformed") from None
        return AnalysisResult(slot.provider, slot.name, model, analysis, incoming, outgoing)
