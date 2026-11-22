"""Metadata-only, secret-redacted check of configured GitHub Actions slots."""
from __future__ import annotations

import os

from .provider_adapters import Adapter, configured_slots


def run(env: dict[str, str], adapter: Adapter | None = None) -> dict[str, str]:
    adapter = adapter or Adapter()
    results: dict[str, str] = {}
    for slot in configured_slots(env):
        try:
            results[slot.name] = adapter.probe(slot)
        except Exception:
            # Never output exception text or response bodies, which may include
            # credential material from untrusted providers.
            results[slot.name] = "unavailable"
    return results


if __name__ == "__main__":
    import json

    print(json.dumps(run(dict(os.environ)), sort_keys=True), flush=True)
