import json
from datetime import datetime, timezone

from scripts.private_ai_ops_hourly_replay import replay


def test_replay_existing_data_shape_clean_restore(tmp_path):
    source = tmp_path / "evidence"
    day = source / "prediction_lab" / "raw" / "2020-09-13"
    day.mkdir(parents=True)
    captured = datetime(2020, 9, 13, 12, 20, tzinfo=timezone.utc).isoformat()
    (day / "evt.json").write_text(json.dumps({"schema": "plane-alerts-prediction-evidence-v1",
        "event_id": "evt-safe", "case_id": "case-safe", "captured_at": captured,
        "kind": "prediction", "state": "CANCELLED", "observer_latitude": 39.654321}))
    output = tmp_path / "snapshot.json"
    events, packets, digest = replay(source, "2020-09-13T12:00:00Z", "a"*40, output)
    assert (events, packets) == (1, 1)
    assert len(digest) == 64 and "observer_latitude" not in output.read_text()
