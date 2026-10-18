import json
from datetime import datetime, timezone

import pytest

from app.private_ops.collector import collect
from app.private_ops.prediction_source import read_hour
from app.private_ops.store import Store

START = 1_600_000_200 // 3600 * 3600


def _raw(event_id, *, kind="prediction", state="APPROACHING"):
    return {"schema": "plane-alerts-prediction-evidence-v1", "kind": kind,
            "event_id": event_id, "case_id": "case-hash",
            "captured_at": datetime.fromtimestamp(START+20, timezone.utc).isoformat(),
            "state": state, "observer_latitude": 38.012345, "observer_longitude": -72.012345}


def test_existing_source_json_and_bundle_dedupe_and_no_private_coordinates(tmp_path):
    day = datetime.fromtimestamp(START, timezone.utc).strftime("%Y-%m-%d")
    source = tmp_path / "checkout" / "prediction_lab" / "raw" / day
    source.mkdir(parents=True)
    (source / "a.json").write_text(json.dumps(_raw("evt-one", state="CANCELLED")))
    (source / "bundle.ndjson").write_text(json.dumps(_raw("evt-one", state="CANCELLED"))+"\n"+json.dumps(_raw("evt-two", state="QUALIFIED"))+"\n")
    events = list(read_hour(tmp_path / "checkout", START, START+3600))
    assert len(events) == 2 and all("observer_latitude" not in e for e in events)
    dbroot = tmp_path / "store"
    dbroot.mkdir()
    store = Store(dbroot)
    result = collect(store, start=START, end=START+3600, records=events, source_complete=True)
    assert result.packets == 1
    store.close()


def test_missing_source_day_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(read_hour(tmp_path, START, START+3600))
