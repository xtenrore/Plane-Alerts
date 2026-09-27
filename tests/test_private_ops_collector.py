import pytest

from app.private_ops.collector import MAX_EVENTS, collect, adapt_prediction_lab_record
from app.private_ops.store import Store
from app.private_ops.dr_export import export, restore_empty

START = 1_600_000_200  # exact UTC hour boundary
START = START - START % 3600


def event(name, case="case:hash", kind="prediction", state="", at=START+5, coverage="good", **other):
    return {"event_id": name, "case_ref": case, "kind": kind, "state": state,
            "at": at, "coverage": coverage, **other}


def run(store, events, *, start=START, complete=True):
    return collect(store, start=start, end=start+3600, records=events, source_complete=complete)


def test_empty_and_normal_windows_skip_ai_and_checkpoint(tmp_path):
    store = Store(tmp_path)
    assert run(store, []).packets == 0
    assert run(store, [event("n", at=START+3605)], start=START+3600).packets == 0
    assert store.db.execute("SELECT checkpoint FROM ai_ops_scheduler WHERE name='collector_hour'").fetchone()[0] == str(START+7200)
    store.close()


@pytest.mark.parametrize("events,reason", [
    ([event("c", state="CANCELLED")], "cancellation"),
    ([event("c", state="CANCELLED"), event("r", state="QUALIFIED", at=START+15)], "requalification_after_cancellation"),
    ([event("a", kind="trajectory")], "trajectory_changed"),
    ([event("a", kind="provider")], "provider_changed"),
    ([event("a", coverage="missing")], "missing_coverage"),
])
def test_reproducible_compact_packet_and_restart(events, reason, tmp_path):
    store = Store(tmp_path)
    assert run(store, events + [events[0]]).packets == 1
    row = store.db.execute("SELECT packet_json,content_hash FROM ai_ops_evidence").fetchone()
    assert reason in row[0]
    assert "latitude" not in row[0] and "token" not in row[0]
    snapshot, manifest = export(store, source_commit="a" * 40)
    store.close()
    store = Store(tmp_path)
    assert run(store, events).packets == 0
    assert store.db.execute("SELECT count(*) FROM ai_ops_evidence").fetchone()[0] == 1
    store.close()
    clean = tmp_path / "clean"
    clean.mkdir()
    restored = restore_empty(clean, snapshot, manifest)
    assert restored.db.execute("SELECT content_hash FROM ai_ops_evidence").fetchone()[0] == row[1]
    restored.close()


def test_incomplete_conflict_or_huge_hour_never_advances_checkpoint(tmp_path):
    store = Store(tmp_path)
    for entries, complete in [([event("x", state="CANCELLED")], False),
                              ([event("x"), event("x", kind="provider")], True),
                              ([event(f"x{i}") for i in range(MAX_EVENTS+1)], True)]:
        with pytest.raises(ValueError):
            run(store, entries, complete=complete)
        assert store.db.execute("SELECT count(*) FROM ai_ops_evidence").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM ai_ops_scheduler WHERE name='collector_hour'").fetchone()[0] == 0
    store.close()


def test_real_prediction_lab_record_adapter_drops_private_coordinates():
    source = {"event_id": "evt-hash", "case_id": "case-hash", "captured_at": "2020-09-13T12:00:01Z",
              "kind": "prediction", "state": "APPROACHING", "projected_closest_km": 3.2,
              "observer_latitude": 39.123456, "observer_longitude": -71.123456, "route_reason": "PRIVATE"}
    event_out = adapt_prediction_lab_record(source)
    assert event_out["cpa_km"] == 3.2
    assert "observer_latitude" not in event_out and "PRIVATE" not in repr(event_out)
