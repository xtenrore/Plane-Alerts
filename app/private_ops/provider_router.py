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
            raise ValueError("explicit free-route approval required")
        for provider, model in self.free:
            if provider not in PROVIDERS or not model or (provider == "openrouter" and not (model.endswith(":free") or model == "openrouter/free")):
                raise ValueError("unapproved or paid analysis route")

    def _usage(self, task: AnalysisTask, slot: Slot, model: str, latency_ms: int, *, success: bool,
               failure_kind: str | None = None, input_tokens: int = 0, output_tokens: int = 0) -> None:
        with self.store.transaction() as db:
            db.execute("""INSERT INTO ai_ops_usage(batch_id,packet_id,task_role,provider,model,key_slot,
                latency_ms,success,failure_kind,malformed,input_tokens,output_tokens,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task.batch_id or None, task.packet_id or None, task.purpose, slot.provider, model, slot.name,
                 max(0, min(3_600_000, int(latency_ms))), 1 if success else 0, failure_kind,
                 1 if failure_kind in ("malformed", "schema") else 0, input_tokens, output_tokens, time.time()))
            # Keep detailed provider usage bounded; canonical finding/review history lives elsewhere.
            db.execute("DELETE FROM ai_ops_usage WHERE created < ?", (time.time() - 30 * 86400,))
            db.execute("""DELETE FROM ai_ops_usage WHERE id NOT IN (
                SELECT id FROM ai_ops_usage ORDER BY id DESC LIMIT 20000)""")
            self.store._mark_dr_dirty(db)

    def execute(self, task: AnalysisTask, models: Mapping[str, str], *, now: float | None = None,
                preferred_providers: Iterable[str] | None = None, exclude_providers: Iterable[str] = (),
                max_attempts: int = 20) -> AnalysisResult:
        now = time.time() if now is None else now
        if max_attempts < 1 or max_attempts > 32:
            raise ValueError("bounded provider attempts required")
        excluded = frozenset(exclude_providers)
        preferred = tuple(preferred_providers or PROVIDERS)
        rank = {p: i for i, p in enumerate(preferred)}
        indexed = list(enumerate(self.slots))
        eligible = [(i, slot) for i, slot in indexed if slot.provider not in excluded
                    and (slot.provider, models.get(slot.provider, "")) in self.free
                    and (task.purpose == "supervisor" or not self.supervisor_reserve or slot.provider != "cloudflare")]
        eligible.sort(key=lambda pair: (rank.get(pair[1].provider, len(rank)), pair[0]))
        if not eligible:
            raise NoFreeRoute("no approved free analysis route")
        attempted = 0
        # Snapshot provider circuit state at the start of this routing attempt. A
        # circuit opened by one key during this call applies only to later calls;
        # it must never skip still-untried independent key slots in the same pool.
        initial_circuit_open: dict[str, bool] = {}
        provider_wide_limited: set[str] = set()
        for _, slot in eligible:
            if attempted >= max_attempts:
                break
            provider = slot.provider
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO ai_ops_provider_health(slot,provider) VALUES(?,?)", (slot.name, provider))
                db.execute("INSERT OR IGNORE INTO ai_ops_provider_circuit(provider) VALUES(?)", (provider,))
                key = db.execute("SELECT cooldown_until FROM ai_ops_provider_health WHERE slot=?", (slot.name,)).fetchone()
                if provider not in initial_circuit_open:
                    circuit = db.execute("SELECT open_until FROM ai_ops_provider_circuit WHERE provider=?", (provider,)).fetchone()
                    initial_circuit_open[provider] = bool(circuit and circuit[0] > now)
                if key[0] > now or initial_circuit_open[provider] or provider in provider_wide_limited:
                    continue
            attempted += 1
            started = time.monotonic()
            try:
                result = self.adapter.execute(slot, task, models[provider], now=now)
            except ProviderFailure as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                self._usage(task, slot, models[provider], elapsed, success=False, failure_kind=exc.kind)
                with self.store.transaction() as db:
                    # Only explicitly slot-scoped 429 responses permit trying a
                    # peer credential. Account-wide quota must stop the whole pool.
                    cooldown = now + (exc.retry_after or 300) if exc.kind == "quota" else now + 900 if exc.kind == "auth" else now
                    db.execute("""UPDATE ai_ops_provider_health SET attempts=attempts+1,failures=failures+1,
                        consecutive_failures=consecutive_failures+1,cooldown_until=max(cooldown_until,?),
                        last_status=?,updated=? WHERE slot=?""", (cooldown, exc.kind, now, slot.name))
                    if exc.kind in ("server", "network", "timeout"):
                        # Only repeated provider transport failures open a short provider circuit.
                        db.execute("""UPDATE ai_ops_provider_circuit SET consecutive_failures=consecutive_failures+1,
                            open_until=CASE WHEN consecutive_failures+1>=3 THEN ? ELSE open_until END,
                            updated=? WHERE provider=?""", (now + 300, now, provider))
                    if exc.kind == "quota" and exc.provider_wide:
                        provider_wide_limited.add(provider)
                        db.execute("UPDATE ai_ops_provider_circuit SET open_until=max(open_until,?),updated=? WHERE provider=?",
                                   (cooldown, now, provider))
                continue
            elapsed = int((time.monotonic() - started) * 1000)
            self._usage(task, slot, models[provider], elapsed, success=True,
                        input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            with self.store.transaction() as db:
                db.execute("""UPDATE ai_ops_provider_health SET attempts=attempts+1,successes=successes+1,
                    input_tokens=input_tokens+?,output_tokens=output_tokens+?,consecutive_failures=0,
                    cooldown_until=0,last_status='healthy',updated=? WHERE slot=?""",
                    (result.input_tokens, result.output_tokens, now, slot.name))
                db.execute("""UPDATE ai_ops_provider_circuit SET consecutive_failures=0,open_until=0,updated=?
                    WHERE provider=?""", (now, provider))
            return result
        raise NoFreeRoute("approved free analysis routes are cooling down or unhealthy")

    @staticmethod
    def _state(last_status: str, cooldown_until: float, now: float) -> str:
        if last_status == "healthy": return "HEALTHY"
        if last_status == "auth": return "AUTH_FAILED"
        if last_status == "quota": return "RATE_LIMITED" if cooldown_until > now else "PROBING"
        if last_status in ("server", "network", "timeout"): return "DEGRADED"
        if cooldown_until > now: return "COOLDOWN"
        return "PROBING"

    def health(self, *, now: float | None = None) -> list[dict[str, object]]:
        now = time.time() if now is None else now
        rows = [dict(row) for row in self.store.db.execute("""SELECT slot,provider,attempts,successes,failures,
            input_tokens,output_tokens,cooldown_until,last_status FROM ai_ops_provider_health ORDER BY provider,slot""")]
        for row in rows:
            row["state"] = self._state(str(row["last_status"]), float(row["cooldown_until"]), now)
        return rows
