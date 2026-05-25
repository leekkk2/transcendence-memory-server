"""Container metadata table (LanceDB-backed, 单独 table 隔离于 memory table)。

设计要点
========
- table 名固定为 ``container_metadata``，存放跨容器的命名规范元数据。
- 与每个容器自己的 ``WS/tasks/rag/containers/<c>/lancedb/`` 物理隔离 —— metadata
  独占一个 LanceDB connection（``db_uri``），由 server 端约定为
  ``WS/tasks/rag/meta/lancedb``。
- LanceDB 0.30 schema 是固定的、首次 create 后不支持 add_column 之外的演进；
  字段一次性写完整。
- ``policy`` 与 ``tags`` 在表里都是 JSON 序列化的 string，避免 LanceDB
  在 list[string] 与 struct 上的版本兼容陷阱。Python 层 upsert / get 时透明
  encode/decode。

公开 API
========
``ContainerMetadata(db_uri)`` —— 实例化一个绑定到具体 LanceDB 目录的句柄。
- ``upsert(name, **fields)`` —— 按 name 主键 upsert，返回写入后的完整行。
- ``get(name)`` —— 返回单行 dict 或 None。
- ``list_all()`` —— 返回全部行（按 name 排序）。
- ``delete(name)`` —— 返回是否真的删了一行。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

import lancedb
import pyarrow as pa


TABLE_NAME = "container_metadata"

# 持久化字段顺序 = pyarrow schema 顺序。新字段必须追加在末尾以保持向后兼容。
_SCHEMA = pa.schema(
    [
        ("name", pa.string()),
        ("description", pa.string()),
        ("tags_json", pa.string()),  # JSON-encoded list[str]
        ("scope", pa.string()),
        ("entity", pa.string()),
        ("purpose", pa.string()),
        ("owner", pa.string()),
        ("created_at", pa.string()),  # ISO8601 UTC string
        ("updated_at", pa.string()),
        ("archived_at", pa.string()),  # 可为空字符串
        ("policy_json", pa.string()),  # JSON-encoded dict
    ]
)

# 公开给调用方的字段（含解码后的 tags/policy）。delete/get/list 输出统一这套形状。
_PUBLIC_FIELDS = (
    "name",
    "description",
    "tags",
    "scope",
    "entity",
    "purpose",
    "owner",
    "created_at",
    "updated_at",
    "archived_at",
    "policy",
)


def _utcnow_iso() -> str:
    """统一 ISO8601 UTC 时间戳格式，与现有 last_modified 字段对齐。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode_row(name: str, fields: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """把外部 dict 转为 LanceDB 行（tags / policy 序列化为 JSON string）。"""
    now = _utcnow_iso()
    if existing is None:
        created_at = now
    else:
        created_at = existing.get("created_at") or now

    def _val(key: str, default: str = "") -> str:
        if key in fields and fields[key] is not None:
            return fields[key]
        if existing is not None:
            return existing.get(key, default) or default
        return default

    tags = fields.get("tags")
    if tags is None and existing is not None:
        tags = existing.get("tags", [])
    tags_json = json.dumps(tags or [], ensure_ascii=False)

    policy = fields.get("policy")
    if policy is None and existing is not None:
        policy = existing.get("policy", {})
    policy_json = json.dumps(policy or {}, ensure_ascii=False)

    archived_at = fields.get("archived_at")
    if archived_at is None and existing is not None:
        archived_at = existing.get("archived_at", "") or ""

    return {
        "name": name,
        "description": _val("description"),
        "tags_json": tags_json,
        "scope": _val("scope"),
        "entity": _val("entity"),
        "purpose": _val("purpose"),
        "owner": _val("owner"),
        "created_at": created_at,
        "updated_at": now,
        "archived_at": archived_at or "",
        "policy_json": policy_json,
    }


def _decode_row(raw: dict[str, Any]) -> dict[str, Any]:
    """把 LanceDB 行转为外部 dict（tags / policy 反序列化）。"""
    try:
        tags = json.loads(raw.get("tags_json") or "[]")
        if not isinstance(tags, list):
            tags = []
    except (TypeError, ValueError):
        tags = []
    try:
        policy = json.loads(raw.get("policy_json") or "{}")
        if not isinstance(policy, dict):
            policy = {}
    except (TypeError, ValueError):
        policy = {}
    archived_at = raw.get("archived_at") or None
    return {
        "name": raw.get("name", ""),
        "description": raw.get("description") or "",
        "tags": tags,
        "scope": raw.get("scope") or "",
        "entity": raw.get("entity") or "",
        "purpose": raw.get("purpose") or "",
        "owner": raw.get("owner") or "",
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or "",
        "archived_at": archived_at,
        "policy": policy,
    }


class ContainerMetadata:
    """LanceDB-backed container metadata store.

    线程安全：每次操作都新开 connection / open_table，避免长连接持锁。
    单实例可在多个请求间共享；内置 lock 让 upsert(read-modify-write) 不会被
    并发请求踩到非原子的 delete+add。
    """

    TABLE_NAME = TABLE_NAME

    def __init__(self, db_uri: str):
        self._db_uri = str(db_uri)
        self._lock = threading.Lock()

    # ---- 内部 helper ---------------------------------------------------
    def _connect(self):
        return lancedb.connect(self._db_uri)

    def _open_or_create(self):
        db = self._connect()
        names = self._table_names(db)
        if TABLE_NAME in names:
            return db, db.open_table(TABLE_NAME)
        # 空表：用 pyarrow 的 schema 显式建，避免 LanceDB 推断出 null 列。
        empty = pa.Table.from_pylist([], schema=_SCHEMA)
        table = db.create_table(TABLE_NAME, data=empty, mode="create")
        return db, table

    @staticmethod
    def _table_names(db) -> list[str]:
        """跨 LanceDB 版本拿表名。

        - 0.30+ ``list_tables()`` 返回 ``ListTablesResponse(tables=[...])`` 对象；
        - 老版本 ``table_names()`` 返回 list[str]，但已 deprecated。
        优先 list_tables，再回退 table_names。
        """
        raw: Any
        try:
            raw = db.list_tables()
        except Exception:
            try:
                raw = db.table_names()
            except Exception:
                return []
        # ListTablesResponse(tables=[...]) → 取 .tables
        if hasattr(raw, "tables"):
            raw = raw.tables or []
        out: list[str] = []
        try:
            iterator = list(raw)
        except TypeError:
            return []
        for item in iterator:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, (list, tuple)) and item:
                out.append(str(item[0]))
            elif isinstance(item, dict):
                out.append(str(item.get("name") or item.get("table_name") or ""))
            else:
                n = getattr(item, "name", "")
                if n:
                    out.append(str(n))
        return [n for n in out if n]

    # ---- 公开 API -----------------------------------------------------
    def upsert(self, name: str, **fields: Any) -> dict[str, Any]:
        """按 name 主键 upsert。返回写入后的完整行。

        - tags / policy 透明序列化为 JSON。
        - created_at 在首次写入时落盘，后续更新不变。
        - updated_at 每次写入都刷新。
        """
        if not name:
            raise ValueError("name is required")
        with self._lock:
            _, table = self._open_or_create()
            existing_raw = self._raw_get(table, name)
            existing = _decode_row(existing_raw) if existing_raw else None
            row = _encode_row(name, fields, existing)
            # LanceDB 0.30 没有原子 upsert，用 delete + add 模拟。
            if existing_raw is not None:
                table.delete(f"name = '{_escape(name)}'")
            table.add([row])
            return _decode_row(row)

    def get(self, name: str) -> Optional[dict[str, Any]]:
        if not name:
            return None
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return None
        table = db.open_table(TABLE_NAME)
        raw = self._raw_get(table, name)
        return _decode_row(raw) if raw else None

    def list_all(self) -> list[dict[str, Any]]:
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return []
        table = db.open_table(TABLE_NAME)
        try:
            rows = table.to_pandas().to_dict("records")
        except Exception:
            # pandas not installed → 回退到 arrow.to_pylist
            rows = table.to_arrow().to_pylist()
        decoded = [_decode_row(r) for r in rows]
        decoded.sort(key=lambda r: r["name"])
        return decoded

    def delete(self, name: str) -> bool:
        if not name:
            return False
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return False
        table = db.open_table(TABLE_NAME)
        if self._raw_get(table, name) is None:
            return False
        table.delete(f"name = '{_escape(name)}'")
        return True

    # ---- 内部读 helper ------------------------------------------------
    @staticmethod
    def _raw_get(table, name: str) -> Optional[dict[str, Any]]:
        try:
            df = table.search().where(f"name = '{_escape(name)}'", prefilter=True).limit(1).to_list()
        except Exception:
            # 无 vector 列时 LanceDB 不允许走 search()；改用 to_pandas filter。
            try:
                rows = table.to_pandas()
                rows = rows[rows["name"] == name].to_dict("records")
            except Exception:
                rows = []
            return rows[0] if rows else None
        return df[0] if df else None


def _escape(value: str) -> str:
    """LanceDB SQL string literal 转义 —— 把 ' 复制成 ''。"""
    return value.replace("'", "''")


__all__ = ["ContainerMetadata", "TABLE_NAME"]
