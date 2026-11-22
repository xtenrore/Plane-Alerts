from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-railway.yml"


def _requirements_lock_pattern() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"requirements\.lock\).*?grep -Eq '([^']+)'",
        text,
        flags=re.DOTALL,
    )
    assert match is not None, "Railway workflow must classify requirements.lock changes"
    return match.group(1)


def _grep(pattern: str, line: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["grep", "-Eq", pattern],
        input=line + "\n",
        text=True,
        capture_output=True,
        check=False,
    )


def test_railway_lockfile_classifier_detects_added_and_removed_packages() -> None:
    pattern = _requirements_lock_pattern()
    for line in ("+astral==3.2", "-geohash2==1.1", "+google-genai==1.2.0"):
        result = _grep(pattern, line)
        assert result.returncode == 0, result.stderr


def test_railway_lockfile_classifier_ignores_comments_and_blank_diff_lines() -> None:
    pattern = _requirements_lock_pattern()
    for line in ("+# comment", "-# old comment", "+"):
        result = _grep(pattern, line)
        assert result.returncode == 1, result.stderr
