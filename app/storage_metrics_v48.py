"""Bounded Mongo/storage observability for Plane Alerts v4.8."""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any

_READ_COMMANDS = {"find", "getmore", "aggregate", "count", "countdocuments", "distinct", "ping"}
_WRITE_COMMANDS = {"insert", "update", "delete", "findandmodify", "bulkwrite", "createindexes", "dropindexes"}


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return float(ordered[index])


class StorageMetrics:
    """Thread-safe bounded metrics; never stores query/document contents."""

    def __init__(self, max_samples: int = 512) -> None:
        self._lock = threading.Lock()
        self._reads: deque[float] = deque(maxlen=max_samples)
        self._writes: deque[float] = deque(maxlen=max_samples)
        self.database_state = "starting"
        self.failures = 0
        self.timeouts = 0
        self.retries = 0
        self.reconnects = 0
        self.optional_writes_dropped = 0
        self.critical_writes_dropped = 0
        self.last_success_epoch = 0.0
        self.schema_version = 0
        self._last_failure_epoch = 0.0

    def set_state(self, state: str) -> None:
        with self._lock:
            self.database_state = str(state)

    def set_schema_version(self, version: int) -> None:
        with self._lock:
            self.schema_version = int(version)

    def record_command(self, command_name: str, duration_ms: float, success: bool, *, timeout: bool = False) -> None:
        name = str(command_name or "").lower()
        duration = max(0.0, float(duration_ms))
        with self._lock:
            if name in _WRITE_COMMANDS:
                self._writes.append(duration)
            elif name in _READ_COMMANDS or name:
                self._reads.append(duration)
            if success:
                self.last_success_epoch = time.time()
                if self.database_state in {"starting", "recovering"}:
                    self.database_state = "healthy"
            else:
                self.failures += 1
                self._last_failure_epoch = time.time()
                if timeout:
                    self.timeouts += 1
                if self.database_state == "healthy":
                    self.database_state = "degraded"

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_reconnect(self) -> None:
        with self._lock:
            self.reconnects += 1
            self.database_state = "recovering"

    def record_drop(self, *, optional: bool) -> None:
        with self._lock:
            if optional:
                self.optional_writes_dropped += 1
            else:
                self.critical_writes_dropped += 1

    @staticmethod
    def _distribution(values: list[float]) -> dict[str, float | int]:
        return {
            "samples": len(values),
            "p50_ms": round(_percentile(values, 0.50), 2),
            "p95_ms": round(_percentile(values, 0.95), 2),
            "p99_ms": round(_percentile(values, 0.99), 2),
            "max_ms": round(max(values), 2) if values else 0.0,
        }

    def snapshot(self, *, write_queue_depth: int = 0, optional_queue_depth: int = 0) -> dict[str, Any]:
        with self._lock:
            reads = list(self._reads)
            writes = list(self._writes)
            payload = {
                "database_state": self.database_state,
                "read_latency": self._distribution(reads),
                "write_latency": self._distribution(writes),
                "failures": self.failures,
                "timeouts": self.timeouts,
                "retry_count": self.retries,
                "reconnects": self.reconnects,
                "optional_writes_dropped": self.optional_writes_dropped,
                "critical_writes_dropped": self.critical_writes_dropped,
                "last_success": self.last_success_epoch or None,
                "schema_version": self.schema_version,
                "write_queue_depth": int(write_queue_depth),
                "optional_queue_depth": int(optional_queue_depth),
            }
        return payload


storage_metrics = StorageMetrics()
