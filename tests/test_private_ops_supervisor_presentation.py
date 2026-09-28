"""Regression coverage for the owner-only Supervisor Chat presentation layer."""

from app.private_ops.supervisor_web_ui import SUPERVISOR_HTML


def test_supervisor_assistant_messages_use_safe_local_markdown_renderer():
    # The prior UI escaped every assistant message wholesale, so **bold** and
    # Markdown tables/code rendered as literal punctuation. Keep formatting local
    # (no CDN dependency), escape input first, then apply the bounded renderer.
    assert "function markdown(raw)" in SUPERVISOR_HTML
    assert "function inlineMarkdown(raw)" in SUPERVISOR_HTML
    assert "s=esc(s)" in SUPERVISOR_HTML
    assert "<strong>$1</strong>" in SUPERVISOR_HTML
    assert "<table><thead><tr>" in SUPERVISOR_HTML
    assert "markdown(m.content)" in SUPERVISOR_HTML
    assert "<script src=" not in SUPERVISOR_HTML.lower()


def test_supervisor_chat_shows_observable_execution_activity_inline():
    # This is an auditable execution/activity view, not hidden chain-of-thought.
    # It reuses already-sanitized trace data and polls while the POST is running so
    # completed read-tool operations can appear in the conversation before the
    # final model answer arrives.
    assert "Execution trace" in SUPERVISOR_HTML
    assert "Ran read tool" in SUPERVISOR_HTML
    assert "Supervisor request in progress" in SUPERVISOR_HTML
    assert "startPolling()" in SUPERVISOR_HTML
    assert "setInterval(()=>load(true),650)" in SUPERVISOR_HTML
    assert "/api/supervisor/trace?conversation_id=" in SUPERVISOR_HTML
    assert "hidden chain-of-thought is not stored or exposed" in SUPERVISOR_HTML
