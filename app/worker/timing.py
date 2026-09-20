"""Bounded measurements of full iterations, including heartbeat/storage work."""
from collections import deque
from contextlib import contextmanager
import time

_durations = deque(maxlen=720)
_intervals = deque(maxlen=720)
_phases: dict[str, deque] = {}
_last_start = None
_last_completed = None


def start_cycle():
    global _last_start
    now = time.monotonic()
    if _last_start is not None:
        _intervals.append((now - _last_start) * 1000)
    _last_start = now
    return now


def finish_cycle(start):
    global _last_completed
    _durations.append((time.monotonic() - start) * 1000)
    _last_completed = time.time()


@contextmanager
def phase(name):
    start = time.monotonic()
    try:
        yield
    finally:
        _phases.setdefault(name, deque(maxlen=720)).append((time.monotonic() - start) * 1000)


def _summary(values):
    data = sorted(values)
    if not data:
        return {"samples": 0}
    return {"samples": len(data), "last_ms": round(values[-1], 1),
            "p50_ms": round(data[int((len(data)-1)*.50)], 1),
            "p95_ms": round(data[int((len(data)-1)*.95)], 1),
            "p99_ms": round(data[int((len(data)-1)*.99)], 1), "max_ms": round(data[-1], 1)}


def snapshot():
    return {"execution": _summary(_durations), "start_interval": _summary(_intervals),
            "phases": {key: _summary(value) for key, value in _phases.items()},
            "last_completed_at": _last_completed}
