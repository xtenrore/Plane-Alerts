from __future__ import annotations

import json
import pathlib

import pytest

from scripts.free_maintenance_agent import (
    AgentError,
    choose_provider,
    discover_cases,
    extract_patch_paths,
    parse_model_json,
    redact_secrets,
    validate_patch,
)


def test_choose_provider_prefers_freellmapi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLMAPI_BASE_URL", "https://router.example")
    monkeypatch.setenv("FREELLMAPI_API_KEY", "free-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    provider = choose_provider()
    assert provider.name == "freellmapi"
    assert provider.base_url == "https://router.example/v1/chat/completions"
    assert provider.api_key == "free-key"


def test_choose_provider_accepts_legacy_openrouter_name(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FREELLMAPI_BASE_URL",
        "FREELLMAPI_API_KEY",
        "OPENROUTER_API_KEY",
        "GROQ_KEY",
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API", "legacy-key")
    provider = choose_provider()
    assert provider.name == "openrouter"
    assert provider.model == "openrouter/free"


def test_redact_secrets_hides_common_credentials() -> None:
    text = "API_KEY=abc123 password=hunter2 mongodb+srv://u:p@example/db Authorization: Bearer secret"
    redacted = redact_secrets(text)
    assert "abc123" not in redacted
    assert "hunter2" not in redacted
    assert "mongodb+srv://" not in redacted
    assert "Bearer secret" not in redacted


def test_discover_cases_is_bounded_and_reads_newest(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "unchecked"
    root.mkdir()
    for idx in range(3):
        path = root / f"case{idx}.json"
        path.write_text(json.dumps({"case_id": f"C{idx}", "value": idx}), encoding="utf-8")
        path.touch()
    bundle = discover_cases(root, limit=2)
    assert len(bundle.paths) == 2
    assert len(bundle.case_ids) == 2
    assert all(cid.startswith("C") for cid in bundle.case_ids)


def test_parse_model_json_strips_markdown_fence() -> None:
    payload = {
        "verdict": "investigate",
        "confidence": "medium",
        "summary": "needs replay",
        "case_ids": ["C1"],
        "suspected_files": ["app/example.py"],
        "recommended_tests": ["tests/test_example.py"],
        "patch": "",
    }
    result = parse_model_json("```json\n" + json.dumps(payload) + "\n```")
    assert result["verdict"] == "investigate"


def test_validate_patch_requires_regression_test_for_app_change() -> None:
    patch = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -1 +1 @@
-old
+new
"""
    with pytest.raises(AgentError, match="regression-test"):
        validate_patch(patch)


def test_validate_patch_accepts_small_app_and_test_patch() -> None:
    patch = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -1 +1 @@
-old
+new
diff --git a/tests/test_a.py b/tests/test_a.py
--- a/tests/test_a.py
+++ b/tests/test_a.py
@@ -1 +1 @@
-old
+new
"""
    assert validate_patch(patch) == ("app/a.py", "tests/test_a.py")


def test_validate_patch_rejects_workflow_or_config_changes() -> None:
    patch = """diff --git a/.github/workflows/tests.yml b/.github/workflows/tests.yml
--- a/.github/workflows/tests.yml
+++ b/.github/workflows/tests.yml
@@ -1 +1 @@
-old
+new
"""
    with pytest.raises(AgentError, match="forbidden"):
        validate_patch(patch)


def test_extract_patch_paths_handles_new_files() -> None:
    patch = """diff --git a/dev/null b/tests/test_new.py
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1 @@
+def test_ok(): pass
"""
    assert extract_patch_paths(patch) == ("tests/test_new.py",)


def test_workflow_wires_github_router_secrets_before_railway_fallback() -> None:
    repo = pathlib.Path(__file__).resolve().parents[1]
    workflow = (repo / ".github/workflows/free-maintenance-agent.yml").read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in workflow
    assert "OPENROUTER_API: ${{ secrets.OPENROUTER_API }}" in workflow
    assert "FREELLMAPI_API_KEY: ${{ secrets.FREELLMAPI_API_KEY }}" in workflow
    assert "existing = os.environ.get(name, '').strip()" in workflow
    assert "exported.append(f'{name}:github')" in workflow
