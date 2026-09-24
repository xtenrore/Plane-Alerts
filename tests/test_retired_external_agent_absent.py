from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", ".venv", "venv", "node_modules"}
FORBIDDEN = (
    "a" + "gy",
    "anti" + "gravity",
    "chatgpt_" + "handoff_json",
)


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
        for needle in FORBIDDEN:
            if needle in rel_text or needle in text:
                matches.append(f"{rel}: {needle}")
    assert not matches, "retired external-agent residue found:\n" + "\n".join(matches)
