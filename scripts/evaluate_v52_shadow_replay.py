#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.shadow_evaluation_v52 import evaluate_replay_cases  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    source = Path(args[0]) if args else ROOT / "tests" / "fixtures" / "v52_shadow_replay.json"
    cases = json.loads(source.read_text(encoding="utf-8"))
    result = evaluate_replay_cases(cases)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))

    # The fixture deliberately contains two scoreable observed passes and two
    # unresolved cases. Each scoreable snapshot yields production + two v4.6
    # candidates, while unresolved cancellation/coverage-loss cases yield none.
    if result.get("skipped_unscoreable_cases") != 2:
        return 2
    models = result.get("models") or []
    if len(models) != 3:
        return 3
    if not any(item.get("model_role") == "production-control" for item in models):
        return 4
    if not any(item.get("model_role") == "shadow-candidate" for item in models):
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
