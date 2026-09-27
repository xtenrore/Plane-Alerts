"""Metadata-only, secret-redacted check of configured GitHub Actions slots."""
from __future__ import annotations

import os

from .provider_adapters import Adapter, configured_slots


class ExistingClientMetadataTransport:
    """Match Plane Alerts' current Groq httpx client for metadata checks."""

    def get_status(self, url: str, headers: dict[str, str]) -> int:
        import httpx

        with httpx.Client(timeout=httpx.Timeout(8.0), follow_redirects=False) as client:
            return client.get(url, headers=headers).status_code


def run(env: dict[str, str], adapter: Adapter | None = None) -> dict[str, str]:
    adapter = adapter or Adapter(ExistingClientMetadataTransport())
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
