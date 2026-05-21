"""Per-container 索引状态机 —— 容器 embedding 索引新鲜度的可观测投影。

历史上 ``/containers`` 的 ``indexed`` 字段只表示「lancedb 目录非空」，无法回答
「索引是否最新」「多少对象待重试」「是否被配额卡住」。本模块补上一个**对已鉴权
agent 透明**的状态机：

状态枚举（优先级高 → 低）：

- ``error``          —— 存在永久失败（dead-letter）对象，需人工介入。
- ``quota_blocked``  —— 有对象因上游配额耗尽待重试，已排长延迟重试。
- ``backlog``        —— 有对象在静默重试积压中（超时 / 瞬时错误）。
- ``indexing``       —— 该容器当前有 embed job 在跑。
- ``stale``          —— 有对象未嵌入且不在 backlog（如迁移前老表待补 hash）。
- ``fresh``          —— 全部对象已嵌入、backlog 空。
- ``unknown``        —— 状态未知（容器从未 embed 过 / 无任何记录）。

``container_index_state`` 表只持久化「子进程才知道的事实」（上次成功 / 尝试
embed 的时间戳、对象计数）；``state`` 字段本身永远由 API 用 ``compute_index_state``
**实时计算**，避免缓存与真实状态失同步。

R8：纯通用开源代码。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from embedding_errors import QUOTA  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - package import path
    from scripts.embedding_errors import QUOTA  # type: ignore[import-not-found]

# 状态常量
FRESH = "fresh"
INDEXING = "indexing"
BACKLOG = "backlog"
QUOTA_BLOCKED = "quota_blocked"
ERROR = "error"
STALE = "stale"
UNKNOWN = "unknown"

ALL_STATES = (ERROR, QUOTA_BLOCKED, BACKLOG, INDEXING, STALE, FRESH, UNKNOWN)


def compute_index_state(
    *,
    total_objects: int,
    embedded_objects: int,
    backlog_active: int,
    dead_count: int,
    job_running: bool,
    last_error_class: str | None,
    ever_embedded: bool,
) -> str:
    """纯函数：按计数 + 运行态推导容器索引状态。

    优先级：error > quota_blocked > backlog > indexing > stale > fresh > unknown。
    子进程、worker、API 三处共用此函数，保证状态判定一致。
    """
    if dead_count > 0:
        return ERROR
    if backlog_active > 0:
        return QUOTA_BLOCKED if last_error_class == QUOTA else BACKLOG
    if job_running:
        return INDEXING
    # backlog 空、无 job：看对象是否全部嵌入
    if total_objects > 0 and embedded_objects < total_objects:
        return STALE
    if not ever_embedded and total_objects > 0:
        # 有对象但从未 embed 过 —— 还没跑过索引
        return STALE
    if total_objects == 0 and not ever_embedded:
        return UNKNOWN
    return FRESH


@dataclass
class IndexStateRecord:
    container: str
    total_objects: int
    embedded_objects: int
    last_embed_ok_at: int | None
    last_embed_attempt_at: int | None
    last_error_class: str | None
    updated_at: int


def _row_to_record(row: sqlite3.Row) -> IndexStateRecord:
    return IndexStateRecord(
        container=row["container"],
        total_objects=int(row["total_objects"]),
        embedded_objects=int(row["embedded_objects"]),
        last_embed_ok_at=(
            int(row["last_embed_ok_at"])
            if row["last_embed_ok_at"] is not None
            else None
        ),
        last_embed_attempt_at=(
            int(row["last_embed_attempt_at"])
            if row["last_embed_attempt_at"] is not None
            else None
        ),
        last_error_class=row["last_error_class"],
        updated_at=int(row["updated_at"]),
    )


class IndexStateStore:
    """``container_index_state`` 表的持久化门面。与 BacklogStore 共用 queue.db。"""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._init_lock, self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS container_index_state (
                    container TEXT PRIMARY KEY,
                    total_objects INTEGER NOT NULL DEFAULT 0,
                    embedded_objects INTEGER NOT NULL DEFAULT 0,
                    last_embed_ok_at INTEGER,
                    last_embed_attempt_at INTEGER,
                    last_error_class TEXT,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def record_embed_run(
        self,
        container: str,
        *,
        total_objects: int,
        embedded_objects: int,
        succeeded: bool,
        last_error_class: str | None = None,
    ) -> None:
        """embed 子进程 / retry job 跑完后登记一次结果（幂等 upsert）。

        ``succeeded`` 为真时刷新 ``last_embed_ok_at``；无论成败都刷新
        ``last_embed_attempt_at``。
        """
        now = int(time.time())
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT last_embed_ok_at FROM container_index_state WHERE container=?",
                (container,),
            ).fetchone()
            ok_at = now if succeeded else (
                existing["last_embed_ok_at"] if existing else None
            )
            conn.execute(
                """INSERT INTO container_index_state
                   (container, total_objects, embedded_objects, last_embed_ok_at,
                    last_embed_attempt_at, last_error_class, updated_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(container) DO UPDATE SET
                     total_objects=excluded.total_objects,
                     embedded_objects=excluded.embedded_objects,
                     last_embed_ok_at=excluded.last_embed_ok_at,
                     last_embed_attempt_at=excluded.last_embed_attempt_at,
                     last_error_class=excluded.last_error_class,
                     updated_at=excluded.updated_at""",
                (
                    container, int(total_objects), int(embedded_objects),
                    ok_at, now, last_error_class, now,
                ),
            )

    def get(self, container: str) -> IndexStateRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM container_index_state WHERE container=?",
                (container,),
            ).fetchone()
            return _row_to_record(row) if row else None

    def all(self) -> list[IndexStateRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM container_index_state ORDER BY container"
            ).fetchall()
            return [_row_to_record(r) for r in rows]
