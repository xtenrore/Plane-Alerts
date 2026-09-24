from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from app import prediction_lab_audit
from app import prediction_lab_files_v55


def test_audit_queue_is_bounded_and_optional():
    work = prediction_lab_audit.audit_work
    assert work.max_pending <= 64
    assert work.timeout <= 3.0
    assert work.semaphore is not None


@pytest.mark.asyncio
async def test_append_evidence_moves_filesystem_work_off_event_loop(monkeypatch, tmp_path):
    original = prediction_lab_files_v55.write_evidence

    def slow_write(doc, *, root=None):
        time.sleep(0.05)
        return original(doc, root=root)

    monkeypatch.setattr(prediction_lab_files_v55, "write_evidence", slow_write)
    started = time.perf_counter()
    task = asyncio.create_task(prediction_lab_files_v55.append_evidence({"kind": "cadence-test"}, root=tmp_path))
    await asyncio.sleep(0.01)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.04
    assert not task.done()
    assert await task is not None


def test_live_monitor_never_calls_github_or_railway_sync():
    source = inspect.getsource(prediction_lab_audit)
    assert "github" not in source.lower()
    assert "railway" not in source.lower()
