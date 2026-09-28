import json

import pytest

from app.private_ops.provider_adapters import (Adapter, AnalysisTask, ProviderFailure, Slot,
                                               configured_slots, retry_after, schema_test_mode, HttpxTransport)
from app.private_ops.provider_router import NoFreeRoute, Router
from app.private_ops.store import Store

TASK = AnalysisTask("triage", "Summarize synthetic audit evidence as JSON")


class FakeHTTP:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, headers, body):
        self.calls.append((url, headers, json.loads(body)))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def get_status(self, url, headers):
        self.calls.append((url, headers, None))
        return next(self.responses)


def ok(provider):
    content = json.dumps({"summary": "synthetic audit", "findings": []})
    if provider == "gemini":
        return 200, {}, json.dumps({"candidates": [{"content": {"parts": [{"text": content}]}}],
                                     "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4}}).encode()
    if provider == "cloudflare":
        return 200, {}, json.dumps({"result": {"response": content}}).encode()
    return 200, {}, json.dumps({"choices": [{"message": {"content": content}}],
                                "usage": {"prompt_tokens": 8, "completion_tokens": 4}}).encode()


@pytest.mark.parametrize("provider,name,model,url_part", [
    ("groq", "GROQ_KEY", "approved-model", "api.groq.com/openai/v1/chat"),
    ("mistral", "MISTRAL_API", "approved-model", "api.mistral.ai/v1/chat"),
    ("gemini", "GEMINI_API_KEY", "approved-model", "generativelanguage.googleapis.com/v1beta/models"),
    ("cloudflare", "CLOUDFLARE_API_TOKEN", "@cf/test/free-model", "accounts/"),
    ("openrouter", "OPENROUTER_API", "test/model:free", "openrouter.ai/api/v1/chat"),
])
def test_provider_wire_shape_and_normalized_result(provider, name, model, url_part):
    transport = FakeHTTP([ok(provider)])
    slot = Slot(provider, name, "sensitive-test-value", "a" * 32 if provider == "cloudflare" else "")
    result = Adapter(transport).execute(slot, TASK, model, now=0)
    assert result.analysis == {"summary": "synthetic audit", "findings": []}
    assert result.slot_name == name and url_part in transport.calls[0][0]
    if provider in ("groq", "mistral"):
        assert transport.calls[0][2]["response_format"] == {"type": "json_object"}
    assert "sensitive-test-value" not in repr(slot) and "sensitive-test-value" not in repr(result)
    if provider == "gemini":
        assert transport.calls[0][1]["x-goog-api-key"] == slot.credential
    elif provider == "cloudflare":
        assert "a" * 32 in transport.calls[0][0]
    else:
        assert transport.calls[0][1]["Authorization"].endswith(slot.credential)


def test_current_gemini_interactions_free_route_is_structured_and_never_stored():
    content = json.dumps({"summary": "synthetic audit", "findings": []})
    response = {"steps": [{"type": "model_output", "content": [{"type": "text", "text": content}]}],
                "usage": {"input_tokens": 18, "output_tokens": 6}}
    transport = FakeHTTP([(200, {}, json.dumps(response).encode())])
    result = Adapter(transport).execute(Slot("gemini", "GEMINI_API_KEY", "test-only"),
                                        TASK, "gemini-3.8-flash", now=0)
    url, headers, body = transport.calls[0]
    assert url.endswith("/v1beta/interactions") and headers["x-goog-api-key"] == "test-only"
    assert body["store"] is False and body["model"] == "gemini-3.8-flash"
    assert body["response_format"]["schema"]["properties"]["findings"]["items"]["properties"]["event_ids"] == {
        "type": "array", "items": {"type": "string"}}
    assert result.analysis == {"summary": "synthetic audit", "findings": []}
    assert (result.input_tokens, result.output_tokens) == (18, 6)


def test_pairing_and_paid_routes_fail_closed():
    env = {"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "first",
           "CLOUDFLARE_API_TOKEN_2": "second", "GROQ_KEY": "groq", "GROQ_KEY_5": "groq5"}
    slots = configured_slots(env)
    assert [s.name for s in slots] == ["GROQ_KEY", "GROQ_KEY_5", "CLOUDFLARE_API_TOKEN"]
    assert slots[-1].account_id == "a" * 32
    with pytest.raises(ValueError, match="paid"):
        Adapter(FakeHTTP([])).execute(Slot("openrouter", "OPENROUTER_API", "secret"), TASK, "paid/model", now=0)
    with pytest.raises(ValueError, match="analysis"):
        AnalysisTask("flight_decision", "choose ETA")


def test_key_failover_ledger_and_independent_429_slots(tmp_path):
    store = Store(tmp_path)
    transport = FakeHTTP([(429, {"Retry-After": "75", "X-RateLimit-Scope": "key"}, b""), ok("groq")])
    slots = [Slot("groq", "GROQ_KEY", "first"), Slot("groq", "GROQ_KEY_2", "second")]
    router = Router(store, slots, adapter=Adapter(transport), approved_free_routes={("groq", "approved-model")})
    assert router.execute(TASK, {"groq": "approved-model"}, now=100).slot_name == "GROQ_KEY_2"
    assert len(transport.calls) == 2
    health = {row["slot"]: row for row in router.health(now=100)}
    assert health["GROQ_KEY"]["cooldown_until"] == 175
    assert health["GROQ_KEY_2"]["input_tokens"] == 8
    store.close()

    # An explicitly organization-wide limit stops peer keys until cooldown.
    clean = tmp_path / "second"
    clean.mkdir()
    store = Store(clean)
    transport = FakeHTTP([(429, {"Retry-After": "120", "X-RateLimit-Scope": "organization"}, b""), ok("groq")])
    router = Router(store, slots, adapter=Adapter(transport), approved_free_routes={("groq", "approved-model")})
    with pytest.raises(NoFreeRoute):
        router.execute(TASK, {"groq": "approved-model"}, now=200)
    assert len(transport.calls) == 1
    assert store.db.execute("SELECT open_until FROM ai_ops_provider_circuit WHERE provider='groq'").fetchone()[0] == 320
    store.close()


def test_wrong_key_timeout_server_circuit_malformed_result_and_no_secret_logging(tmp_path):
    store = Store(tmp_path)
    slots = [Slot("mistral", "MISTRAL_API", "SECRET_TEST_ONLY"), Slot("mistral", "MISTRAL_API_2", "OTHER_SECRET")]
    transport = FakeHTTP([(401, {}, b"SECRET_TEST_ONLY"), ProviderFailure("network"),
                          (503, {}, b""), (503, {}, b"")])
    router = Router(store, slots, adapter=Adapter(transport), approved_free_routes={("mistral", "approved-model")})
    with pytest.raises(NoFreeRoute):
        router.execute(TASK, {"mistral": "approved-model"}, now=0)
    assert router.health(now=0)[0]["last_status"] == "auth"
    with pytest.raises(NoFreeRoute):
        router.execute(TASK, {"mistral": "approved-model"}, now=301)
    with pytest.raises(NoFreeRoute):
        router.execute(TASK, {"mistral": "approved-model"}, now=302)
    assert store.db.execute("SELECT open_until FROM ai_ops_provider_circuit WHERE provider='mistral'").fetchone()[0] == 602
    with pytest.raises(ProviderFailure, match="schema"):
        Adapter(FakeHTTP([(200, {}, b'{"choices":[{"message":{"content":"{}"}}]}')])).execute(slots[1], TASK, "approved-model", now=0)
    assert "SECRET_TEST_ONLY" not in repr(router.health(now=0))
    store.close()



def test_current_attempt_exhausts_independent_slots_before_honoring_new_provider_circuit(tmp_path):
    store = Store(tmp_path)
    slots = [Slot("groq", "GROQ_KEY" if i == 1 else f"GROQ_KEY_{i}", f"key-{i}") for i in range(1, 6)]
    # The third transport failure opens the provider circuit for future work, but
    # this in-flight routing attempt must still try slots 4 and 5. Slot 5 succeeds.
    transport = FakeHTTP([(503, {}, b""), (503, {}, b""), (503, {}, b""),
                          (503, {}, b""), ok("groq")])
    router = Router(store, slots, adapter=Adapter(transport), approved_free_routes={("groq", "approved-model")})
    result = router.execute(TASK, {"groq": "approved-model"}, now=500, max_attempts=8)
    assert result.slot_name == "GROQ_KEY_5"
    assert len(transport.calls) == 5
    # Success closes the circuit again.
    assert store.db.execute("SELECT open_until FROM ai_ops_provider_circuit WHERE provider='groq'").fetchone()[0] == 0
    store.close()


def test_context_too_large_recognizes_413_and_common_400_envelope_without_logging_body():
    slot = Slot("groq", "GROQ_KEY", "NEVER_LOG_THIS")
    for response in [
        (413, {}, b""),
        (400, {}, b'{"error":{"code":"context_length_exceeded","message":"maximum context length"}}'),
    ]:
        with pytest.raises(ProviderFailure) as error:
            Adapter(FakeHTTP([response])).execute(slot, TASK, "approved-model", now=0)
        assert error.value.kind == "context"
        assert "NEVER_LOG_THIS" not in repr(error.value)
        assert "maximum context" not in repr(error.value)

def test_retry_after_and_supervisor_reserve(tmp_path):
    assert retry_after({"Retry-After": "999999"}, now=0) == 3600
    assert retry_after({}, now=0) == 60
    store = Store(tmp_path)
    router = Router(store, [Slot("cloudflare", "CLOUDFLARE_API_TOKEN", "secret", "a" * 32)],
                    adapter=Adapter(FakeHTTP([])), approved_free_routes={("cloudflare", "@cf/test/free-model")})
    with pytest.raises(NoFreeRoute):
        router.execute(TASK, {"cloudflare": "@cf/test/free-model"}, now=0)
    store.close()


def test_cross_provider_failover_preserves_durable_job_and_idempotent_step(tmp_path):
    store = Store(tmp_path)
    assert store.enqueue("audit:synthetic")
    assert store.claim("worker") == "audit:synthetic"
    assert store.begin_step("audit:synthetic", "analysis", "worker", {"safe": True})
    slots = [Slot("groq", "GROQ_KEY", "fake-a"), Slot("gemini", "GEMINI_API_KEY", "fake-b")]
    transport = FakeHTTP([(503, {}, b""), ok("gemini")])
    router = Router(store, slots, adapter=Adapter(transport),
                    approved_free_routes={("groq", "approved-model"), ("gemini", "approved-model")})
    result = router.execute(TASK, {"groq": "approved-model", "gemini": "approved-model"}, now=100)
    assert result.provider == "gemini" and len(transport.calls) == 2
    store.complete_step("audit:synthetic", "analysis", "worker", result.analysis)
    store.close()
    reopened = Store(tmp_path)
    assert not reopened.begin_step("audit:synthetic", "analysis", "worker", {"safe": True})
    assert reopened.db.execute("SELECT successes FROM ai_ops_provider_health WHERE slot='GEMINI_API_KEY'").fetchone()[0] == 1
    reopened.close()


def test_capability_schema_mode_and_hard_openrouter_approval(tmp_path):
    assert schema_test_mode("gemini", "approved-model", ok("gemini")[2]) == {"summary": "synthetic audit", "findings": []}
    with pytest.raises(ProviderFailure, match="schema"):
        schema_test_mode("groq", "approved-model", b'{"choices":[{"message":{"content":"{}"}}]}')
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="paid"):
        Router(store, [Slot("openrouter", "OPENROUTER_API", "fake")], adapter=Adapter(FakeHTTP([])),
               approved_free_routes={("openrouter", "paid/model")})
    store.close()


@pytest.mark.parametrize("provider,name", [(p, n) for p, n in [
    ("groq", "GROQ_KEY"), ("mistral", "MISTRAL_API"), ("gemini", "GEMINI_API_KEY"),
    ("cloudflare", "CLOUDFLARE_API_TOKEN"), ("openrouter", "OPENROUTER_API")]])
def test_safe_metadata_probe_does_not_call_inference(provider, name):
    transport = FakeHTTP([200])
    slot = Slot(provider, name, "fake", "a" * 32 if provider == "cloudflare" else "")
    assert Adapter(transport).probe(slot) == "available"
    assert len(transport.calls) == 1 and transport.calls[0][2] is None
    assert "/chat/completions" not in transport.calls[0][0] and "/ai/run/" not in transport.calls[0][0]


def test_default_httpx_transport_matches_existing_production_client(monkeypatch):
    httpx = pytest.importorskip("httpx")

    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda request: (
        seen.append((request.method, request.headers.get("authorization", ""))) or
        httpx.Response(200, json={"data": []})
    )))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client)
    slot = Slot("groq", "GROQ_KEY", "test-only-credential")
    assert Adapter(HttpxTransport()).probe(slot) == "available"
    assert seen == [("GET", "Bearer test-only-credential")]
    client.close()
