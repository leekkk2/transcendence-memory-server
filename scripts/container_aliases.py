"""Container alias table — 客户端旧名透明路由到 canonical 容器（LanceDB-backed）。

设计要点
========
- table 名固定为 ``container_aliases``；与 ``container_metadata`` 共用 db_uri
  （server 端约定 ``WS/tasks/rag/meta/lancedb``），但物理独立一张表。
- 客户端默认 ``container = "sanva-yzjx"``，但 server 端容器经过 DR-047 治理后
  改名 / 合并 / 物理删除。本表负责"接住"旧名：
    * status='active'     → 透传到 canonical（无感）
    * status='deprecated' → 透传到 canonical + 日志告警
    * status='removed'    → 直接 410 GONE，防客户端"幽灵重建"已删容器
- tags / 复杂结构如有 → 走 JSON-string 列（避坑 LanceDB 0.30 list/struct 兼容陷阱，
  Lane A 已验证此模式可行）。本表当前 schema 不含 list/struct，全部为 string。
- 字段一次性写完整；首次 create 后不支持 add_column 之外的演进。

公开 API
========
``ContainerAliases(db_uri)`` —— 实例化一个绑定到具体 LanceDB 目录的句柄。
- ``resolve(alias)``     —— 命中返回行 dict，未命中返回 None。caller 自行判断 status。
- ``upsert(...)``        —— 按 alias 主键 upsert，返回写入后的完整行。
- ``list_all()``         —— 返回全部行（按 alias 排序）。
- ``delete(alias)``      —— 返回是否真的删了一行。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any, Optional

import lancedb
import pyarrow as pa


TABLE_NAME = "container_aliases"

VALID_STATUSES = ("active", "deprecated", "removed")

# 持久化字段顺序 = pyarrow schema 顺序。新字段必须追加在末尾以保持向后兼容。
_SCHEMA = pa.schema(
    [
        ("alias", pa.string()),
        ("canonical", pa.string()),
        ("reason", pa.string()),
        ("status", pa.string()),  # active | deprecated | removed
        ("notes", pa.string()),
        ("created_at", pa.string()),  # ISO8601 UTC string
        ("updated_at", pa.string()),
    ]
)

_PUBLIC_FIELDS = (
    "alias",
    "canonical",
    "reason",
    "status",
    "notes",
    "created_at",
    "updated_at",
)


def _utcnow_iso() -> str:
    """统一 ISO8601 UTC 时间戳格式，与 container_metadata 对齐。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode_row(
    alias: str,
    fields: dict[str, Any],
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """encode a row。fields 只含显式传入的字段（partial update 友好），其余从
    existing 继承；首次插入且 fields 缺 canonical 由调用方提前校验。
    """
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

    canonical = _val("canonical")
    if not canonical:
        raise ValueError("canonical is required")
    reason = _val("reason")
    status = _val("status", default="active")
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}; expected one of {VALID_STATUSES}")
    notes = _val("notes")

    return {
        "alias": alias,
        "canonical": canonical,
        "reason": reason,
        "status": status,
        "notes": notes,
        "created_at": created_at,
        "updated_at": now,
    }


def _decode_row(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "alias": raw.get("alias", ""),
        "canonical": raw.get("canonical") or "",
        "reason": raw.get("reason") or "",
        "status": raw.get("status") or "active",
        "notes": raw.get("notes") or "",
        "created_at": raw.get("created_at") or "",
        "updated_at": raw.get("updated_at") or "",
    }


class ContainerAliases:
    """LanceDB-backed container alias store.

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
        empty = pa.Table.from_pylist([], schema=_SCHEMA)
        table = db.create_table(TABLE_NAME, data=empty, mode="create")
        return db, table

    @staticmethod
    def _table_names(db) -> list[str]:
        """跨 LanceDB 版本拿表名（同 container_metadata 的兼容写法）。"""
        raw: Any
        try:
            raw = db.list_tables()
        except Exception:
            try:
                raw = db.table_names()
            except Exception:
                return []
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
    def resolve(self, name: str) -> Optional[dict[str, Any]]:
        """命中返回完整行 dict；未命中返回 None。

        caller 责任：
          - row is None         → name 本身是 canonical 或未注册的新容器
          - row['status']='removed' → 应该抛 410
          - row['status']='deprecated' → 透传到 canonical 并记 warning
          - row['status']='active'    → 透传到 canonical（无感）
        """
        if not name:
            return None
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return None
        table = db.open_table(TABLE_NAME)
        raw = self._raw_get(table, name)
        return _decode_row(raw) if raw else None

    def upsert(
        self,
        alias: str,
        canonical: Optional[str] = None,
        reason: Optional[str] = None,
        status: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """按 alias 主键 upsert，返回完整行。

        - 首次插入时 canonical 必填；后续 upsert 可省略以保留原值（partial update）。
        - status 必须 ∈ VALID_STATUSES（若给定）。
        - created_at 首次写入时落盘，后续更新不变；updated_at 每次刷新。
        """
        if not alias:
            raise ValueError("alias is required")
        if status is not None and status not in VALID_STATUSES:
            raise ValueError(
                f"invalid status: {status!r}; expected one of {VALID_STATUSES}"
            )
        with self._lock:
            _, table = self._open_or_create()
            existing_raw = self._raw_get(table, alias)
            existing = _decode_row(existing_raw) if existing_raw else None
            # 仅显式传入的字段进 fields；其余靠 _encode_row 从 existing 继承
            fields: dict[str, Any] = {}
            if canonical is not None:
                fields["canonical"] = canonical
            if reason is not None:
                fields["reason"] = reason
            if status is not None:
                fields["status"] = status
            if notes is not None:
                fields["notes"] = notes
            if existing is None and "canonical" not in fields:
                raise ValueError("canonical is required for first insert")
            row = _encode_row(alias, fields, existing)
            if existing_raw is not None:
                table.delete(f"alias = '{_escape(alias)}'")
            table.add([row])
            return _decode_row(row)

    def list_all(self) -> list[dict[str, Any]]:
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return []
        table = db.open_table(TABLE_NAME)
        try:
            rows = table.to_pandas().to_dict("records")
        except Exception:
            rows = table.to_arrow().to_pylist()
        decoded = [_decode_row(r) for r in rows]
        decoded.sort(key=lambda r: r["alias"])
        return decoded

    def delete(self, alias: str) -> bool:
        if not alias:
            return False
        db = self._connect()
        if TABLE_NAME not in self._table_names(db):
            return False
        table = db.open_table(TABLE_NAME)
        if self._raw_get(table, alias) is None:
            return False
        table.delete(f"alias = '{_escape(alias)}'")
        return True

    def aliases_for_canonical(self, canonical: str) -> list[dict[str, Any]]:
        """反向 lookup：给定 canonical name，返回所有指向它的非 removed alias 行。

        用于 /containers 列表的 metadata.aliases 反向附加（让运维一眼看到一个
        canonical 容器被哪些旧名透传）。removed 状态不返回（避免误导，已 410 拒收）。
        """
        if not canonical:
            return []
        out: list[dict[str, Any]] = []
        for row in self.list_all():
            if row["canonical"] == canonical and row["status"] != "removed":
                out.append(row)
        return out

    # ---- 内部读 helper ------------------------------------------------
    @staticmethod
    def _raw_get(table, name: str) -> Optional[dict[str, Any]]:
        try:
            df = (
                table.search()
                .where(f"alias = '{_escape(name)}'", prefilter=True)
                .limit(1)
                .to_list()
            )
        except Exception:
            try:
                rows = table.to_pandas()
                rows = rows[rows["alias"] == name].to_dict("records")
            except Exception:
                rows = []
            return rows[0] if rows else None
        return df[0] if df else None


def _escape(value: str) -> str:
    """LanceDB SQL string literal 转义 —— 把 ' 复制成 ''。"""
    return value.replace("'", "''")


__all__ = ["ContainerAliases", "TABLE_NAME", "VALID_STATUSES"]
