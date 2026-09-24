from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text missing in {path}: {old!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")


replace("app/version.py", 'VERSION = "5.5.0"', 'VERSION = "5.5.1"')
replace(
    "app/version.py",
    "# v5.5 moves Prediction Lab audit/shadow evidence from high-volume Mongo writes\n",
    "# v5.5.1 carries the file-backed Prediction Lab architecture forward because\n# the immutable v5.5.0 tag was already published for guided community self-hosting.\n# It moves Prediction Lab audit/shadow evidence from high-volume Mongo writes\n",
)

p = Path("README.md")
p.write_text(p.read_text(encoding="utf-8").replace("v5.5.0", "v5.5.1"), encoding="utf-8")

p = Path("CHANGELOG.md")
text = p.read_text(encoding="utf-8").replace(
    "## v5.5.0 — File-Backed Prediction Lab & Automated Evidence Pipeline",
    "## v5.5.1 — File-Backed Prediction Lab & Automated Evidence Pipeline",
    1,
)
marker = "\n## v5.4.3 — AGY Removal\n"
community = '''
## v5.5.0 — Guided Community Self-Hosting

- Published the guided community self-hosting installer and stable cross-platform installer assets.
- Kept that public tag immutable; it was deliberately separate from the owner's Railway production deployment path.
- Physical prediction version remained `5.3-3d-proximity-age-aware`.
'''
if community.strip() not in text:
    if marker not in text:
        raise SystemExit("CHANGELOG insertion marker missing")
    text = text.replace(marker, "\n" + community + marker, 1)
p.write_text(text, encoding="utf-8")

source = Path("docs/releases/v5.5.0.md")
release_text = source.read_text(encoding="utf-8").replace("v5.5.0", "v5.5.1")
release_text = release_text.replace(
    "# Plane Alerts v5.5.1 — File-Backed Prediction Lab & Automated Evidence Pipeline\n",
    "# Plane Alerts v5.5.1 — File-Backed Prediction Lab & Automated Evidence Pipeline\n\n> Version note: `v5.5.0` was already published as the separate Guided Community Self-Hosting release. Plane Alerts release rules prohibit moving or replacing that stable tag, so this production architecture ships as the next patch, `v5.5.1`.\n",
    1,
)
Path("docs/releases/v5.5.1.md").write_text(release_text, encoding="utf-8")
source.unlink()

for path in ("tests/test_general_audit_fixes_v542.py", "tests/test_user_experience_v54.py"):
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if '5.5.0' not in text:
        raise SystemExit(f"expected 5.5.0 assertion missing in {path}")
    p.write_text(text.replace('5.5.0', '5.5.1'), encoding="utf-8")
