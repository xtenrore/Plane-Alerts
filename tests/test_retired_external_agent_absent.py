from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules"}
FORBIDDEN = (
    "a" + "gy",
    "anti" + "gravity",
    "chatgpt_" + "handoff_json",
)
# The owner-added canonical private-AI-operations roadmap intentionally documents
# the retired system by name in order to say it is permanently retired.  Keep the
# runtime/source residue guard strict everywhere else rather than deleting or
# rewriting that standing instruction document.
RETIREMENT_DOCUMENTS = {"readthis.md"}


def _repository_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        yield path, text.casefold()


def test_retired_external_agent_has_no_repository_residue():
    matches = []
    for path, text in _repository_text_files():
        rel = path.relative_to(ROOT)
        rel_text = str(rel).casefold()
        if rel_text in RETIREMENT_DOCUMENTS:
            # The exception is valid only while this file explicitly says the old
            # integration is permanently retired; it is not a blanket docs bypass.
            assert "permanently retired" in text
            continue
        for needle in FORBIDDEN:
            if needle in rel_text or needle in text:
                matches.append(f"{rel}: {needle}")
    assert not matches, "retired external-agent residue found:\n" + "\n".join(matches)
