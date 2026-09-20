from pathlib import Path


ENTRYPOINT = Path("scripts/railway-entrypoint.sh")
DOCKERFILE = Path("Dockerfile")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_railway_requires_core_runtime_secrets_before_starting():
    text = _text(ENTRYPOINT)
    assert "RAILWAY_ENVIRONMENT" in text
    for name in ("TELEGRAM_BOT_TOKEN", "MONGO_URI"):
        assert f'${{{name}:-}}' in text
        assert f'missing="$missing {name}"' in text
    assert 'missing="$missing GEMINI_API_KEY"' not in text
    assert "exit 78" in text
    assert "eval " not in text


def test_railway_guard_keeps_gemini_optional_for_deterministic_runtime():
    text = _text(ENTRYPOINT)
    assert '${GEMINI_API_KEY:-}' in text
    assert "Gemini advisor is disabled" in text
    assert "deterministic fallback remains active" in text
    assert "Plane?" not in text
    assert "v3.4 fallback" not in text


def test_railway_guard_does_not_echo_secret_values():
    text = _text(ENTRYPOINT)
    assert 'echo "$TELEGRAM_BOT_TOKEN"' not in text
    assert 'echo "$MONGO_URI"' not in text
    assert 'echo "$GEMINI_API_KEY"' not in text
    assert "Missing:$missing" in text


def test_dockerfile_uses_strict_railway_entrypoint():
    text = _text(DOCKERFILE)
    assert "chmod +x /app/scripts/railway-entrypoint.sh" in text
    assert 'CMD ["/app/scripts/railway-entrypoint.sh"]' in text
