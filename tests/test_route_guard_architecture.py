from __future__ import annotations

from pathlib import Path


CANONICAL = Path("app/intelligence/route_guard.py")
OBSOLETE_MODULE_FILES = (
    Path("app/intelligence/destination_path_guard_v554.py"),
    Path("app/intelligence/route_guard_v2.py"),
    Path("app/intelligence/route_guard_v42.py"),
    Path("app/intelligence/route_guard_v47.py"),
    Path("app/intelligence/requalification_guard_v43.py"),
    Path("app/intelligence/arrival_guard_hotfix.py"),
)
OBSOLETE_IMPORTS = (
    "app.intelligence.destination_path_guard_v554",
    "app.intelligence.route_guard_v2",
    "app.intelligence.route_guard_v42",
    "app.intelligence.route_guard_v47",
    "app.intelligence.requalification_guard_v43",
    "app.intelligence.arrival_guard_hotfix",
)


def test_route_guard_has_one_canonical_production_module():
    assert CANONICAL.is_file()
    for path in OBSOLETE_MODULE_FILES:
        assert not path.exists(), f"obsolete route-guard module returned: {path}"


def test_obsolete_route_guard_generations_are_not_imported_again():
    roots = (Path("app"), Path("tests"), Path("scripts"))
    this_file = Path(__file__).resolve()
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            if path.resolve() == this_file:
                continue
            text = path.read_text(encoding="utf-8")
            for old_import in OBSOLETE_IMPORTS:
                if old_import in text:
                    offenders.append(f"{path}: {old_import}")
    assert offenders == [], "obsolete route-guard import(s): " + "; ".join(offenders)


def test_worker_binds_only_the_canonical_route_guard_module():
    text = Path("app/worker/__init__.py").read_text(encoding="utf-8")
    assert "from app.intelligence.route_guard import install_destination_path_guard" in text
    for old_import in OBSOLETE_IMPORTS:
        assert old_import not in text
