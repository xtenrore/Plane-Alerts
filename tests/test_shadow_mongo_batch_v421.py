from pathlib import Path

from app import shadow_mongo_batch_v421 as batch


class _FakeCursor:
    def __init__(self):
        self.batch_size_value = None

    def batch_size(self, value):
        self.batch_size_value = value
        return self


class _FakeCollection:
    def __init__(self):
        self.cursor = _FakeCursor()
        self.find_args = None
        self.find_kwargs = None
        self.marker = object()

    def find(self, *args, **kwargs):
        self.find_args = args
        self.find_kwargs = kwargs
        return self.cursor


class _FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, _FakeCollection())


def test_heavy_shadow_collections_use_small_cursor_batches():
    database = _FakeDatabase()
    proxy = batch._DatabaseProxy(database)

    history_cursor = proxy["flight_route_samples"].find({"utc_date": {"$in": ["2026-09-18"]}})
    sentinel_cursor = proxy["prediction_sentinel_routes"].find({"utc_date": {"$gte": "2026-09-16"}})
    audit_cursor = proxy["prediction_lab_audit"].find({"status": "awaiting_outcome"})

    assert history_cursor.batch_size_value == 12
    assert sentinel_cursor.batch_size_value == 12
    assert audit_cursor.batch_size_value == 32


def test_collection_proxy_preserves_non_find_methods_and_query_arguments():
    database = _FakeDatabase()
    proxy = batch._DatabaseProxy(database)
    collection = proxy["flight_route_samples"]

    cursor = collection.find({"utc_date": "2026-09-18"}, {"points": 1})
    raw = database["flight_route_samples"]

    assert raw.find_args == ({"utc_date": "2026-09-18"}, {"points": 1})
    assert cursor is raw.cursor
    assert collection.marker is raw.marker


def test_v421_wrapper_keeps_shadow_logic_modules_unchanged():
    source = Path("app/shadow_mongo_batch_v421.py").read_text()
    assert "next_hour_quality.update_next_hour_shadow" in source
    assert "sentinel_base.update_sentinel_shadow" in source
    assert '"flight_route_samples": 12' in source
    assert '"prediction_sentinel_routes": 12' in source
