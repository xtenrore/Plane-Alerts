"""SQLite-backed async document store for Plane Alerts community self-hosting.

The live Plane Alerts code historically uses a small Mongo-style async API. This
module implements the subset required by Plane Alerts on top of one durable
SQLite file so the rest of the application can keep its deterministic runtime
and restart-safety semantics without pretending SQLite is a Mongo URI.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterable
from uuid import uuid4

try:
    from bson import ObjectId
except Exception:  # pragma: no cover - pymongo is a normal Plane Alerts dependency
    ObjectId = None  # type: ignore[assignment]


_MISSING = object()
_TYPE_KEY = "$plane_alerts_type"


def default_sqlite_path() -> Path:
    """Return a persistent fallback path outside the replaceable source tree."""
    explicit_dir = os.getenv("PLANE_ALERTS_DATA_DIR", "").strip()
    if explicit_dir:
        return Path(explicit_dir).expanduser() / "planealerts.db"
    if os.name == "nt":
        root = (
            os.getenv("PROGRAMDATA", "").strip()
            or os.getenv("LOCALAPPDATA", "").strip()
            or str(Path.home())
        )
        return Path(root) / "PlaneAlerts" / "data" / "planealerts.db"
    xdg = os.getenv("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "plane-alerts" / "planealerts.db"
    return Path.home() / ".local" / "share" / "plane-alerts" / "planealerts.db"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {_TYPE_KEY: "datetime", "value": _aware(value).isoformat()}
    if ObjectId is not None and isinstance(value, ObjectId):
        return {_TYPE_KEY: "objectid", "value": str(value)}
    if isinstance(value, bytes):
        return {_TYPE_KEY: "bytes", "value": value.hex()}
    if isinstance(value, tuple):
        return {_TYPE_KEY: "tuple", "value": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {_TYPE_KEY: "string", "value": str(value)}


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {_TYPE_KEY, "value"}:
        kind = value.get(_TYPE_KEY)
        raw = value.get("value")
        if kind == "datetime":
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return _aware(parsed)
        if kind == "objectid" and ObjectId is not None:
            try:
                return ObjectId(str(raw))
            except Exception:
                return str(raw)
        if kind == "bytes":
            try:
                return bytes.fromhex(str(raw))
            except ValueError:
                return b""
        if kind == "tuple":
            return tuple(_decode(v) for v in (raw or []))
        if kind == "string":
            return str(raw)
    return {str(k): _decode(v) for k, v in value.items()}


def _dumps(document: dict[str, Any]) -> str:
    return json.dumps(_encode(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _loads(payload: str) -> dict[str, Any]:
    decoded = _decode(json.loads(payload))
    return decoded if isinstance(decoded, dict) else {}


def _get(document: Any, path: str) -> Any:
    current = document
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _set(document: dict[str, Any], path: str, value: Any) -> None:
    parts = str(path).split(".")
    current = document
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = deepcopy(value)


def _unset(document: dict[str, Any], path: str) -> None:
    parts = str(path).split(".")
    current: Any = document
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def _cmp(left: Any, right: Any, op: str) -> bool:
    if left is _MISSING:
        return op == "$ne"
    try:
        if op == "$eq":
            return left == right
        if op == "$ne":
            return left != right
        if op == "$gt":
            return left > right
        if op == "$gte":
            return left >= right
        if op == "$lt":
            return left < right
        if op == "$lte":
            return left <= right
    except TypeError:
        return False
    return False


def _value_matches(value: Any, condition: Any) -> bool:
    if isinstance(condition, dict) and any(str(k).startswith("$") for k in condition):
        for op, wanted in condition.items():
            if op == "$exists":
                if (value is not _MISSING) != bool(wanted):
                    return False
            elif op == "$in":
                options = list(wanted or [])
                if isinstance(value, list):
                    if not any(item in options for item in value):
                        return False
                elif value not in options:
                    return False
            elif op == "$nin":
                options = list(wanted or [])
                if isinstance(value, list):
                    if any(item in options for item in value):
                        return False
                elif value in options:
                    return False
            elif op in {"$eq", "$ne", "$gt", "$gte", "$lt", "$lte"}:
                if not _cmp(value, wanted, op):
                    return False
            elif op == "$not":
                if _value_matches(value, wanted):
                    return False
            else:
                raise NotImplementedError(f"SQLite backend does not support query operator {op}")
        return True
    if value is _MISSING:
        return condition is None
    if isinstance(value, list) and not isinstance(condition, list):
        return condition in value
    return value == condition


def _matches(document: dict[str, Any], query: dict[str, Any] | None) -> bool:
    query = query or {}
    for key, condition in query.items():
        if key == "$or":
            if not any(_matches(document, branch) for branch in (condition or [])):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, branch) for branch in (condition or [])):
                return False
            continue
        if key == "$nor":
            if any(_matches(document, branch) for branch in (condition or [])):
                return False
            continue
        if str(key).startswith("$"):
            raise NotImplementedError(f"SQLite backend does not support query operator {key}")
        if not _value_matches(_get(document, str(key)), condition):
            return False
    return True


def _project(document: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    if not projection:
        return deepcopy(document)
    included = [str(k) for k, v in projection.items() if bool(v) and k != "_id"]
    excluded = [str(k) for k, v in projection.items() if not bool(v)]
    if included:
        out: dict[str, Any] = {}
        if projection.get("_id", 1) and "_id" in document:
            out["_id"] = deepcopy(document["_id"])
        for path in included:
            value = _get(document, path)
            if value is not _MISSING:
                _set(out, path, value)
        return out
    out = deepcopy(document)
    for path in excluded:
        _unset(out, path)
    return out


def _normalise_sort(spec: Any, direction: int | None = None) -> list[tuple[str, int]]:
    if isinstance(spec, str):
        return [(spec, int(direction or 1))]
    if isinstance(spec, tuple) and len(spec) == 2 and isinstance(spec[0], str):
        return [(str(spec[0]), int(spec[1]))]
    if isinstance(spec, Iterable):
        result: list[tuple[str, int]] = []
        for item in spec:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                result.append((str(item[0]), int(item[1])))
        return result
    return []


def _sort_value(value: Any) -> tuple[int, Any]:
    if value is _MISSING or value is None:
        return (0, "")
    if isinstance(value, datetime):
        return (1, _aware(value).timestamp())
    if isinstance(value, (int, float, str)):
        return (1, value)
    return (1, str(value))


def _apply_update(document: dict[str, Any], update: dict[str, Any], *, inserting: bool) -> dict[str, Any]:
    if not any(str(k).startswith("$") for k in update):
        replacement = deepcopy(update)
        if "_id" not in replacement and "_id" in document:
            replacement["_id"] = document["_id"]
        return replacement
    out = deepcopy(document)
    for path, value in (update.get("$set") or {}).items():
        _set(out, str(path), value)
    if inserting:
        for path, value in (update.get("$setOnInsert") or {}).items():
            _set(out, str(path), value)
    for path in (update.get("$unset") or {}):
        _unset(out, str(path))
    for path, delta in (update.get("$inc") or {}).items():
        current = _get(out, str(path))
        current = 0 if current is _MISSING or current is None else current
        _set(out, str(path), current + delta)
    for path, value in (update.get("$max") or {}).items():
        current = _get(out, str(path))
        if current is _MISSING or current < value:
            _set(out, str(path), value)
    for path, value in (update.get("$min") or {}).items():
        current = _get(out, str(path))
        if current is _MISSING or current > value:
            _set(out, str(path), value)
    for path, value in (update.get("$push") or {}).items():
        current = _get(out, str(path))
        values = [] if current is _MISSING or not isinstance(current, list) else list(current)
        if isinstance(value, dict) and "$each" in value:
            values.extend(deepcopy(list(value.get("$each") or [])))
            if "$slice" in value:
                slice_n = int(value["$slice"])
                values = values[:slice_n] if slice_n >= 0 else values[slice_n:]
        else:
            values.append(deepcopy(value))
        _set(out, str(path), values)
    for path, value in (update.get("$addToSet") or {}).items():
        current = _get(out, str(path))
        values = [] if current is _MISSING or not isinstance(current, list) else list(current)
        candidates = list(value.get("$each") or []) if isinstance(value, dict) and "$each" in value else [value]
        for candidate in candidates:
            if candidate not in values:
                values.append(deepcopy(candidate))
        _set(out, str(path), values)
    for path, wanted in (update.get("$pull") or {}).items():
        current = _get(out, str(path))
        if isinstance(current, list):
            _set(out, str(path), [item for item in current if not _value_matches(item, wanted)])
    for path, enabled in (update.get("$currentDate") or {}).items():
        if enabled:
            _set(out, str(path), datetime.now(timezone.utc))
    supported = {
        "$set", "$setOnInsert", "$unset", "$inc", "$max", "$min",
        "$push", "$addToSet", "$pull", "$currentDate",
    }
    unknown = {str(k) for k in update if str(k).startswith("$")} - supported
    if unknown:
        raise NotImplementedError(
            "SQLite backend does not support update operator(s): " + ", ".join(sorted(unknown))
        )
    return out


class SQLiteCursor:
    def __init__(self, collection: "SQLiteCollection", query: dict[str, Any] | None, projection: dict[str, Any] | None) -> None:
        self._collection = collection
        self._query = deepcopy(query or {})
        self._projection = deepcopy(projection) if projection else None
        self._sort: list[tuple[str, int]] = []
        self._limit = 0
        self._skip = 0

    def sort(self, spec: Any, direction: int | None = None) -> "SQLiteCursor":
        self._sort = _normalise_sort(spec, direction)
        return self

    def limit(self, value: int) -> "SQLiteCursor":
        self._limit = max(0, int(value))
        return self

    def skip(self, value: int) -> "SQLiteCursor":
        self._skip = max(0, int(value))
        return self

    async def _rows(self) -> list[dict[str, Any]]:
        docs = await self._collection._database._find_documents(self._collection.name, self._query)
        for field, direction in reversed(self._sort):
            docs.sort(key=lambda item, f=field: _sort_value(_get(item, f)), reverse=int(direction) < 0)
        if self._skip:
            docs = docs[self._skip:]
        if self._limit:
            docs = docs[:self._limit]
        return [_project(doc, self._projection) for doc in docs]

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def generate() -> AsyncIterator[dict[str, Any]]:
            for row in await self._rows():
                yield row
        return generate()

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        rows = await self._rows()
        return rows if length is None else rows[: max(0, int(length))]


class SQLiteCollection:
    def __init__(self, database: "SQLiteDatabase", name: str) -> None:
        self._database = database
        self.name = str(name)

    def find(self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None) -> SQLiteCursor:
        return SQLiteCursor(self, query, projection)

    async def find_one(self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None, *, sort: Any = None) -> dict[str, Any] | None:
        cursor = self.find(query, projection)
        if sort:
            cursor.sort(sort)
        cursor.limit(1)
        rows = await cursor.to_list(1)
        return rows[0] if rows else None

    async def insert_one(self, document: dict[str, Any]) -> Any:
        doc = deepcopy(document)
        doc.setdefault("_id", uuid4().hex)
        await self._database._insert_document(self.name, doc)
        return SimpleNamespace(inserted_id=doc["_id"], acknowledged=True)

    async def insert_many(self, documents: Iterable[dict[str, Any]], ordered: bool = True) -> Any:
        ids = []
        for document in documents:
            result = await self.insert_one(document)
            ids.append(result.inserted_id)
        return SimpleNamespace(inserted_ids=ids, acknowledged=True)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], *, upsert: bool = False) -> Any:
        return await self._database._update_documents(self.name, query, update, multi=False, upsert=upsert)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any], *, upsert: bool = False) -> Any:
        return await self._database._update_documents(self.name, query, update, multi=True, upsert=upsert)

    async def replace_one(self, query: dict[str, Any], replacement: dict[str, Any], *, upsert: bool = False) -> Any:
        return await self._database._replace_one(self.name, query, replacement, upsert=upsert)

    async def delete_one(self, query: dict[str, Any]) -> Any:
        return await self._database._delete_documents(self.name, query, multi=False)

    async def delete_many(self, query: dict[str, Any]) -> Any:
        return await self._database._delete_documents(self.name, query, multi=True)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return len(await self._database._find_documents(self.name, query))

    async def estimated_document_count(self) -> int:
        return len(await self._database._find_documents(self.name, {}))

    async def distinct(self, key: str, query: dict[str, Any] | None = None) -> list[Any]:
        values: list[Any] = []
        for document in await self._database._find_documents(self.name, query or {}):
            value = _get(document, key)
            if value is _MISSING:
                continue
            candidates = value if isinstance(value, list) else [value]
            for candidate in candidates:
                if candidate not in values:
                    values.append(candidate)
        return values

    async def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any], *, upsert: bool = False, return_document: Any = False, projection: dict[str, Any] | None = None) -> dict[str, Any] | None:
        before = await self.find_one(query)
        await self.update_one(query, update, upsert=upsert)
        if before is None and not upsert:
            return None
        if bool(return_document):
            identity = {"_id": before["_id"]} if before and "_id" in before else query
            after = await self.find_one(identity)
            return _project(after, projection) if after is not None else None
        return _project(before, projection) if before is not None else None

    async def find_one_and_delete(self, query: dict[str, Any], *, projection: dict[str, Any] | None = None) -> dict[str, Any] | None:
        before = await self.find_one(query)
        if before is not None:
            await self.delete_one({"_id": before.get("_id")})
        return _project(before, projection) if before is not None else None

    async def create_index(self, keys: Any, *, unique: bool = False, expireAfterSeconds: int | float | None = None, name: str | None = None, **_: Any) -> str:
        fields = _normalise_sort(keys)
        if not fields and isinstance(keys, str):
            fields = [(keys, 1)]
        index_name = name or "_".join(f"{field}_{direction}" for field, direction in fields)
        await self._database._create_index(self.name, index_name, fields, unique=bool(unique), expire_after=expireAfterSeconds)
        return index_name

    async def drop(self) -> None:
        await self._database._drop_collection(self.name)


class SQLiteDatabase:
    """Small async Mongo-shaped database facade backed by SQLite."""

    is_plane_alerts_sqlite = True

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS plane_documents (
                collection TEXT NOT NULL,
                doc_key TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (collection, doc_key)
            )
        """)
        self._connection.execute("""
            CREATE TABLE IF NOT EXISTS plane_indexes (
                collection TEXT NOT NULL,
                name TEXT NOT NULL,
                fields_json TEXT NOT NULL,
                unique_flag INTEGER NOT NULL DEFAULT 0,
                expire_after REAL,
                PRIMARY KEY (collection, name)
            )
        """)
        self._lock = asyncio.Lock()
        self._collections: dict[str, SQLiteCollection] = {}

    def __getitem__(self, name: str) -> SQLiteCollection:
        key = str(name)
        if key not in self._collections:
            self._collections[key] = SQLiteCollection(self, key)
        return self._collections[key]

    async def command(self, command: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        name = str(command if isinstance(command, str) else next(iter(command or {}), ""))
        if name == "ping":
            await self._execute_scalar("SELECT 1")
            return {"ok": 1.0}
        raise NotImplementedError(f"SQLite backend does not support database command {name!r}")

    async def list_collection_names(self) -> list[str]:
        def op() -> list[str]:
            rows = self._connection.execute("SELECT DISTINCT collection FROM plane_documents ORDER BY collection").fetchall()
            return [str(row[0]) for row in rows]
        return await self._run(op)

    async def integrity_check(self) -> tuple[bool, str]:
        def op() -> tuple[bool, str]:
            row = self._connection.execute("PRAGMA integrity_check").fetchone()
            detail = str(row[0] if row else "unknown")
            return detail.lower() == "ok", detail
        return await self._run(op)

    async def backup_to(self, target: str | os.PathLike[str]) -> Path:
        destination = Path(target).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        def op() -> None:
            backup = sqlite3.connect(destination, timeout=5.0)
            try:
                self._connection.backup(backup)
            finally:
                backup.close()
        await self._run(op)
        return destination

    async def close(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._connection.close)

    async def _run(self, func):
        async with self._lock:
            return await asyncio.to_thread(func)

    async def _execute_scalar(self, sql: str) -> Any:
        def op() -> Any:
            row = self._connection.execute(sql).fetchone()
            return row[0] if row else None
        return await self._run(op)

    def _purge_expired_sync(self, collection: str) -> None:
        indexes = self._connection.execute(
            "SELECT fields_json, expire_after FROM plane_indexes WHERE collection=? AND expire_after IS NOT NULL",
            (collection,),
        ).fetchall()
        if not indexes:
            return
        now = datetime.now(timezone.utc)
        rows = self._connection.execute("SELECT doc_key, payload FROM plane_documents WHERE collection=?", (collection,)).fetchall()
        expired: list[str] = []
        for row in rows:
            document = _loads(str(row["payload"]))
            for index in indexes:
                fields = json.loads(str(index["fields_json"]))
                if not fields:
                    continue
                value = _get(document, str(fields[0][0]))
                if not isinstance(value, datetime):
                    continue
                deadline = _aware(value) + timedelta(seconds=float(index["expire_after"] or 0))
                if deadline <= now:
                    expired.append(str(row["doc_key"]))
                    break
        if expired:
            self._connection.executemany(
                "DELETE FROM plane_documents WHERE collection=? AND doc_key=?",
                [(collection, key) for key in expired],
            )

    def _all_sync(self, collection: str) -> list[dict[str, Any]]:
        self._purge_expired_sync(collection)
        rows = self._connection.execute("SELECT payload FROM plane_documents WHERE collection=?", (collection,)).fetchall()
        return [_loads(str(row["payload"])) for row in rows]

    async def _find_documents(self, collection: str, query: dict[str, Any] | None) -> list[dict[str, Any]]:
        def op() -> list[dict[str, Any]]:
            return [doc for doc in self._all_sync(collection) if _matches(doc, query)]
        return await self._run(op)

    def _unique_indexes_sync(self, collection: str) -> list[list[tuple[str, int]]]:
        rows = self._connection.execute(
            "SELECT fields_json FROM plane_indexes WHERE collection=? AND unique_flag=1", (collection,)
        ).fetchall()
        return [[(str(f), int(d)) for f, d in json.loads(str(row["fields_json"]))] for row in rows]

    def _assert_unique_sync(self, collection: str, document: dict[str, Any], *, ignore_id: Any = None) -> None:
        for fields in self._unique_indexes_sync(collection):
            wanted = tuple(_get(document, field) for field, _ in fields)
            for existing in self._all_sync(collection):
                if ignore_id is not None and existing.get("_id") == ignore_id:
                    continue
                current = tuple(_get(existing, field) for field, _ in fields)
                if current == wanted:
                    names = ", ".join(field for field, _ in fields)
                    raise ValueError(f"SQLite unique index violation on {collection} ({names})")

    def _write_document_sync(self, collection: str, document: dict[str, Any]) -> None:
        doc = deepcopy(document)
        doc.setdefault("_id", uuid4().hex)
        self._assert_unique_sync(collection, doc, ignore_id=doc.get("_id"))
        key = str(doc["_id"])
        self._connection.execute(
            "INSERT INTO plane_documents(collection, doc_key, payload) VALUES(?,?,?) "
            "ON CONFLICT(collection, doc_key) DO UPDATE SET payload=excluded.payload",
            (collection, key, _dumps(doc)),
        )

    async def _insert_document(self, collection: str, document: dict[str, Any]) -> None:
        def op() -> None:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if any(doc.get("_id") == document.get("_id") for doc in self._all_sync(collection)):
                    raise ValueError(f"Duplicate _id in {collection}")
                self._write_document_sync(collection, document)
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        await self._run(op)

    @staticmethod
    def _seed_from_query(query: dict[str, Any]) -> dict[str, Any]:
        seed: dict[str, Any] = {}
        for key, value in query.items():
            if str(key).startswith("$"):
                continue
            if isinstance(value, dict) and any(str(k).startswith("$") for k in value):
                continue
            _set(seed, str(key), value)
        return seed

    async def _update_documents(self, collection: str, query: dict[str, Any], update: dict[str, Any], *, multi: bool, upsert: bool) -> Any:
        def op() -> Any:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                documents = self._all_sync(collection)
                matching = [doc for doc in documents if _matches(doc, query)]
                targets = matching if multi else matching[:1]
                modified = 0
                for existing in targets:
                    updated = _apply_update(existing, update, inserting=False)
                    updated.setdefault("_id", existing.get("_id") or uuid4().hex)
                    self._write_document_sync(collection, updated)
                    modified += int(updated != existing)
                upserted_id = None
                if not targets and upsert:
                    seed = self._seed_from_query(query)
                    seed.setdefault("_id", uuid4().hex)
                    inserted = _apply_update(seed, update, inserting=True)
                    inserted.setdefault("_id", seed["_id"])
                    self._write_document_sync(collection, inserted)
                    upserted_id = inserted["_id"]
                self._connection.execute("COMMIT")
                return SimpleNamespace(matched_count=len(targets), modified_count=modified, upserted_id=upserted_id, acknowledged=True)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return await self._run(op)

    async def _replace_one(self, collection: str, query: dict[str, Any], replacement: dict[str, Any], *, upsert: bool) -> Any:
        def op() -> Any:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                documents = self._all_sync(collection)
                existing = next((doc for doc in documents if _matches(doc, query)), None)
                upserted_id = None
                matched = 0
                modified = 0
                if existing is not None:
                    doc = deepcopy(replacement)
                    doc.setdefault("_id", existing.get("_id") or uuid4().hex)
                    self._write_document_sync(collection, doc)
                    matched = 1
                    modified = int(doc != existing)
                elif upsert:
                    doc = self._seed_from_query(query)
                    doc.update(deepcopy(replacement))
                    doc.setdefault("_id", uuid4().hex)
                    self._write_document_sync(collection, doc)
                    upserted_id = doc["_id"]
                self._connection.execute("COMMIT")
                return SimpleNamespace(matched_count=matched, modified_count=modified, upserted_id=upserted_id, acknowledged=True)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return await self._run(op)

    async def _delete_documents(self, collection: str, query: dict[str, Any], *, multi: bool) -> Any:
        def op() -> Any:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                matches = [doc for doc in self._all_sync(collection) if _matches(doc, query)]
                targets = matches if multi else matches[:1]
                if targets:
                    self._connection.executemany(
                        "DELETE FROM plane_documents WHERE collection=? AND doc_key=?",
                        [(collection, str(doc.get("_id"))) for doc in targets],
                    )
                self._connection.execute("COMMIT")
                return SimpleNamespace(deleted_count=len(targets), acknowledged=True)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return await self._run(op)

    async def _create_index(self, collection: str, name: str, fields: list[tuple[str, int]], *, unique: bool, expire_after: int | float | None) -> None:
        def op() -> None:
            if unique:
                seen: list[tuple[Any, ...]] = []
                for document in self._all_sync(collection):
                    key = tuple(_get(document, field) for field, _ in fields)
                    if key in seen:
                        raise ValueError(f"Existing records violate unique index {name} on {collection}")
                    seen.append(key)
            self._connection.execute(
                "INSERT INTO plane_indexes(collection,name,fields_json,unique_flag,expire_after) VALUES(?,?,?,?,?) "
                "ON CONFLICT(collection,name) DO UPDATE SET fields_json=excluded.fields_json, "
                "unique_flag=excluded.unique_flag, expire_after=excluded.expire_after",
                (collection, name, json.dumps(fields, separators=(",", ":")), 1 if unique else 0, None if expire_after is None else float(expire_after)),
            )
        await self._run(op)

    async def _drop_collection(self, collection: str) -> None:
        def op() -> None:
            self._connection.execute("DELETE FROM plane_documents WHERE collection=?", (collection,))
            self._connection.execute("DELETE FROM plane_indexes WHERE collection=?", (collection,))
        await self._run(op)


def backend_label(database: Any) -> str:
    return "sqlite" if bool(getattr(database, "is_plane_alerts_sqlite", False)) else "mongodb"
