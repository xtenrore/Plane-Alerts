"""Persistent per-key health and bounded free-route failover for AI analysis."""
from __future__ import annotations

import time
from typing import Iterable, Mapping

from .provider_adapters import Adapter, AnalysisResult, AnalysisTask, ProviderFailure, Slot, PROVIDERS
from .store import Store


class NoFreeRoute(RuntimeError):
    """All explicitly approved free analysis routes are unavailable."""


class Router:
    def __init__(self, store: Store, slots: Iterable[Slot], *, adapter: Adapter,
                 approved_free_routes: Iterable[tuple[str, str]], supervisor_reserve: bool = True):
        self.store = store
        self.slots = tuple(slots)
        self.adapter = adapter
        self.free = frozenset(approved_free_routes)
        self.supervisor_reserve = supervisor_reserve
        if len({s.name for s in self.slots}) != len(self.slots):
            raise ValueError("duplicate credential slot")
        if not self.free:
            # Fail closed: a model name alone is never proof of zero cost.
            raise ValueError("explicit free-route approval required")
        for provider, model in self.free:
            if provider not in PROVIDERS or not model or (provider == "openrouter" and
                    not (model.endswith(":free") or model == "openrouter/free")):
                raise ValueError("unapproved or paid analysis route")

    def execute(self, task: AnalysisTask, models: Mapping[str, str], *, now: float | None = None) -> AnalysisResult:
        now = time.time() if now is None else now
        eligible = [slot for slot in self.slots if (slot.provider, models.get(slot.provider, "")) in self.free
                    and (task.purpose == "supervisor" or not self.supervisor_reserve or slot.provider != "cloudflare")]
        if not eligible:
            raise NoFreeRoute("no approved free analysis route")
        for slot in eligible:
            provider = slot.provider
            with self.store.transaction() as db:
                db.execute("""INSERT OR IGNORE INTO ai_ops_provider_health(slot,provider) VALUES(?,?)""", (slot.name, provider))
                db.execute("""INSERT OR IGNORE INTO ai_ops_provider_circuit(provider) VALUES(?)""", (provider,))
                key = db.execute("SELECT cooldown_until FROM ai_ops_provider_health WHERE slot=?", (slot.name,)).fetchone()
                circuit = db.execute("SELECT open_until FROM ai_ops_provider_circuit WHERE provider=?", (provider,)).fetchone()
                if key[0] > now or circuit[0] > now:
                    continue
            try:
                result = self.adapter.execute(slot, task, models[provider], now=now)
            except ProviderFailure as exc:
                with self.store.transaction() as db:
                    # Provider-wide quota signals stop rotating keys of that
                    # provider; ordinary 429 affects only its own key pool.
                    cooldown = now + (exc.retry_after or 300) if exc.kind == "quota" else now + 900 if exc.kind == "auth" else now
                    db.execute("""UPDATE ai_ops_provider_health SET attempts=attempts+1,failures=failures+1,
                        consecutive_failures=consecutive_failures+1,cooldown_until=max(cooldown_until,?),
                        last_status=?,updated=? WHERE slot=?""", (cooldown, exc.kind, now, slot.name))
                    if exc.provider_wide and exc.kind == "quota":
                        db.execute("""UPDATE ai_ops_provider_circuit SET open_until=?,updated=? WHERE provider=?""",
                                   (now + (exc.retry_after or 300), now, provider))
                    elif exc.kind in ("server", "network"):
                        db.execute("""UPDATE ai_ops_provider_circuit SET consecutive_failures=consecutive_failures+1,
                            open_until=CASE WHEN consecutive_failures+1>=3 THEN ? ELSE open_until END,
                            updated=? WHERE provider=?""", (now + 300, now, provider))
                continue
            with self.store.transaction() as db:
                db.execute("""UPDATE ai_ops_provider_health SET attempts=attempts+1,successes=successes+1,
                    input_tokens=input_tokens+?,output_tokens=output_tokens+?,consecutive_failures=0,
                    cooldown_until=0,last_status='healthy',updated=? WHERE slot=?""",
                    (result.input_tokens, result.output_tokens, now, slot.name))
                db.execute("""UPDATE ai_ops_provider_circuit SET consecutive_failures=0,open_until=0,updated=?
                    WHERE provider=?""", (now, provider))
            return result
        raise NoFreeRoute("approved free analysis routes are cooling down or unhealthy")

    def health(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.store.db.execute("""SELECT slot,provider,attempts,successes,failures,
            input_tokens,output_tokens,cooldown_until,last_status FROM ai_ops_provider_health ORDER BY provider,slot""")]
