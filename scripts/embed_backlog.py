"""Embedding backlog —— 配额 / 超时失败 chunk 的持久化静默重试积压。

设计动机：

记忆系统是长期沉淀、非实时的 —— 今天写入的内容可能几周 / 几月后才被召回。
当 embedding 因上游配额耗尽（free tier 429）或超时失败时，把失败的 chunk
**持久化**进 backlog 而非丢弃；后台按错误类别分级退避**静默重试**，重试可
跨天 / 跨周长期进行。embedding 一切正常时 backlog 恒空 —— 对有付费能力、
不撞配额的部署零开销。

与 ``JobQueue`` 的关系：复用同一个 ``queue.db`` SQLite 文件（单一 WAL /
崩溃恢复 / purge 域），但 backlog 是 job 队列的**卫星**，独立成 ``BacklogStore``
类，不与 ``JobQueue`` 耦合。与 job 队列不同的是：``dead``（永久错误）行**保留**，
作为显式 dead-letter 供状态 API 上报，不被 purge。

backlog 行状态机：

    waiting  ──claim──▶ retrying ──成功──▶ resolved
       ▲                   │
       └──重试失败(可重试)──┘
                           └──永久错误──▶ dead

R8：纯通用开源代码，不含任何 endpoint / 主机名 / 凭证 / 私有容器名。
"""
from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:
    from embedding_errors import (  # type: ignore[import-not-found]
        ALL_CLASSES,
        PERMANENT,
        QUOTA,
        TIMEOUT,
        TRANSIENT,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.embedding_errors import (  # type: ignore[import-not-found]
        ALL_CLASSES,
        PERMANENT,
        QUOTA,
        TIMEOUT,
        TRANSIENT,
    )

logger = logging.getLogger("transcendence-memory-server.backlog")

BACKLOG_STATUSES = {"waiting", "retrying", "resolved", "dead"}


def _parse_schedule(env_value: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """把 ``"90,300,900"`` 形式的 env 解析成退避表；非法值回退默认。"""
    if not env_value:
        return default
    try:
        parsed = tuple(int(x.strip()) for x in env_value.split(",") if x.strip())
        return parsed or default
    except ValueError:
        logger.warning("invalid backlog schedule %r; using default", env_value)
        return default


# 分级退避表（秒）。下标 = 已重试次数；超出表长则停在最后一档无限重复。
# quota：反映 Google free tier 配额「按分钟 → 按天」的重置边界 —— 先分钟级
# 试探，最终落到 ~1 天一次，长期重试不放弃。
# timeout / transient：更短 cadence，多为瞬时问题。
_QUOTA_SCHEDULE = _parse_schedule(
    os.environ.get("TM_BACKLOG_QUOTA_SCHEDULE", ""),
    (90, 300, 900, 3600, 21600, 86400),
)
_TIMEOUT_SCHEDULE = _parse_schedule(
    os.environ.get("TM_BACKLOG_TIMEOUT_SCHEDULE", ""),
    (30, 120, 300, 900, 3600),
)
_TRANSIENT_SCHEDULE = _parse_schedule(
    os.environ.get("TM_BACKLOG_TRANSIENT_SCHEDULE", ""),
    (15, 60, 180, 600, 1800),
)
_SCHEDULES: dict[str, tuple[int, ...]] = {
    QUOTA: _QUOTA_SCHEDULE,
    TIMEOUT: _TIMEOUT_SCHEDULE,
    TRANSIENT: _TRANSIENT_SCHEDULE,
}
_JITTER_PCT = max(0.0, min(0.9, float(os.environ.get("TM_BACKLOG_JITTER_PCT", "20")) / 100.0))


def next_retry_delay(
    error_class: str, attempts: int, retry_after: float | None = None,
) -> float:
    """按错误类别 + 已重试次数算下一次重试延迟（秒）。

    - 退避表按 ``attempts`` 索引，超出表长停在最后一档（长期重试不放弃）。
    - 叠加 ±``TM_BACKLOG_JITTER_PCT`` 抖动，避免多 chunk 同时重试踩同一限速窗口。
    - 上游 ``Retry-After`` 作为**下界** —— 永不早于上游建议的时间重试。
    """
    schedule = _SCHEDULES.get(error_class, _TRANSIENT_SCHEDULE)
    idx = min(max(attempts, 0), len(schedule) - 1)
    base = float(schedule[idx])
    delay = base + base * random.uniform(-_JITTER_PCT, _JITTER_PCT)
    delay = max(1.0, delay)
    if retry_after is not None and retry_after > 0:
        delay = max(delay, retry_after)
    return delay


@dataclass
class BacklogItem:
    id: int
    container: str
    chunk_id: str
    content_hash: str | None
    error_class: str
    attempts: int
    first_failed_at: int
    last_attempt_at: int
    next_retry_at: int
    last_error: str | None
    retry_after_hint: float | None
    status: str
    resolved_at: int | None


def _row_to_item(row: sqlite3.Row) -> BacklogItem:
    return BacklogItem(
        id=int(row["id"]),
        container=row["container"],
        chunk_id=row["chunk_id"],
        content_hash=row["content_hash"],
        error_class=row["error_class"],
        attempts=int(row["attempts"]),
        first_failed_at=int(row["first_failed_at"]),
        last_attempt_at=int(row["last_attempt_at"]),
        next_retry_at=int(row["next_retry_at"]),
        last_error=row["last_error"],
        retry_after_hint=(
            float(row["retry_after_hint"])
            if row["retry_after_hint"] is not None
            else None
        ),
        status=row["status"],
        resolved_at=int(row["resolved_at"]) if row["resolved_at"] is not None else None,
    )


class BacklogStore:
    """``embed_backlog`` 表的持久化门面。线程安全：短连接 + per-call。"""

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
                CREATE TABLE IF NOT EXISTS embed_backlog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    container TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    content_hash TEXT,
                    error_class TEXT NOT NULL DEFAULT 'transient',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    first_failed_at INTEGER NOT NULL,
                    last_attempt_at INTEGER NOT NULL,
                    next_retry_at INTEGER NOT NULL,
                    last_error TEXT,
                    retry_after_hint REAL,
                    status TEXT NOT NULL DEFAULT 'waiting',
                    resolved_at INTEGER,
                    UNIQUE(container, chunk_id)
                );
                CREATE INDEX IF NOT EXISTS idx_backlog_due
                    ON embed_backlog (status, next_retry_at);
                CREATE INDEX IF NOT EXISTS idx_backlog_container
                    ON embed_backlog (container, status);
                """
            )
            # 崩溃恢复：启动时残留 'retrying' 行（拥有它的 retry job 已死）回
            # 'waiting'，让下次 drain 重新拾取 —— 镜像 JobQueue 的 running 恢复。
            cursor = conn.execute(
                """UPDATE embed_backlog SET status='waiting'
                   WHERE status='retrying'"""
            )
            if cursor.rowcount:
                logger.warning(
                    "recovered %d retrying backlog items after restart",
                    cursor.rowcount,
                )

    # ---------- 写入 ----------

    def record_failure(
        self,
        container: str,
        chunk_id: str,
        error_class: str,
        *,
        content_hash: str | None = None,
        last_error: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        """登记 / 更新一个失败 chunk（按 ``(container, chunk_id)`` 幂等 upsert）。

        - 首次失败：插入 ``waiting`` 行，``attempts=1``。
        - 再次失败：``attempts+1``，刷新 ``error_class`` / ``last_error``，按新
          ``attempts`` 重排 ``next_retry_at``；若此前是 ``resolved``/``dead``
          则重新激活为 ``waiting``。
        - ``permanent`` 错误：直接置 ``dead``（不再自动重试，进 dead-letter）。
        """
        if error_class not in ALL_CLASSES:
            error_class = TRANSIENT
        now = int(time.time())
        is_permanent = error_class == PERMANENT
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id, attempts FROM embed_backlog WHERE container=? AND chunk_id=?",
                (container, chunk_id),
            ).fetchone()
            if existing is None:
                status = "dead" if is_permanent else "waiting"
                next_retry = (
                    now
                    if is_permanent
                    else now + int(next_retry_delay(error_class, 0, retry_after))
                )
                conn.execute(
                    """INSERT INTO embed_backlog
                       (container, chunk_id, content_hash, error_class, attempts,
                        first_failed_at, last_attempt_at, next_retry_at,
                        last_error, retry_after_hint, status, resolved_at)
                       VALUES (?,?,?,?,1,?,?,?,?,?,?,NULL)""",
                    (
                        container, chunk_id, content_hash, error_class,
                        now, now, next_retry, last_error, retry_after, status,
                    ),
                )
                return
            attempts = int(existing["attempts"]) + 1
            status = "dead" if is_permanent else "waiting"
            next_retry = (
                now
                if is_permanent
                else now + int(next_retry_delay(error_class, attempts, retry_after))
            )
            conn.execute(
                """UPDATE embed_backlog
                   SET error_class=?, attempts=?, last_attempt_at=?, next_retry_at=?,
                       last_error=?, retry_after_hint=?, status=?, resolved_at=NULL,
                       content_hash=COALESCE(?, content_hash)
                   WHERE id=?""",
                (
                    error_class, attempts, now, next_retry, last_error,
                    retry_after, status, content_hash, existing["id"],
                ),
            )

    def mark_resolved(self, container: str, chunk_id: str) -> None:
        """chunk 重试成功 —— 置 ``resolved``。"""
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """UPDATE embed_backlog SET status='resolved', resolved_at=?,
                      last_error=NULL WHERE container=? AND chunk_id=?""",
                (now, container, chunk_id),
            )

    def mark_resolved_many(self, container: str, chunk_ids: list[str]) -> int:
        """批量标记成功；返回更新行数。"""
        if not chunk_ids:
            return 0
        now = int(time.time())
        with self._conn() as conn:
            total = 0
            for cid in chunk_ids:
                cur = conn.execute(
                    """UPDATE embed_backlog SET status='resolved', resolved_at=?,
                          last_error=NULL WHERE container=? AND chunk_id=?""",
                    (now, container, cid),
                )
                total += cur.rowcount
            return total

    def claim_due(
        self, container: str, limit: int, now: int | None = None,
    ) -> list[BacklogItem]:
        """原子领取某容器到期的 ``waiting`` 行并置 ``retrying``。

        retry job 启动时调用。崩溃后 ``retrying`` 行由 ``_init_schema`` 恢复。
        """
        if now is None:
            now = int(time.time())
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """SELECT * FROM embed_backlog
                       WHERE container=? AND status='waiting' AND next_retry_at<=?
                       ORDER BY next_retry_at ASC, id ASC LIMIT ?""",
                    (container, now, int(limit)),
                ).fetchall()
                ids = [int(r["id"]) for r in rows]
                if ids:
                    conn.execute(
                        f"UPDATE embed_backlog SET status='retrying' "
                        f"WHERE id IN ({','.join('?' * len(ids))})",
                        ids,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return [_row_to_item(r) for r in rows]

    def release_retrying(self, container: str, chunk_ids: list[str]) -> None:
        """把仍 ``retrying`` 的行放回 ``waiting``（retry job 中途异常的兜底）。"""
        if not chunk_ids:
            return
        with self._conn() as conn:
            for cid in chunk_ids:
                conn.execute(
                    """UPDATE embed_backlog SET status='waiting'
                       WHERE container=? AND chunk_id=? AND status='retrying'""",
                    (container, cid),
                )

    # ---------- 查询 ----------

    def due_containers(self, now: int | None = None) -> list[str]:
        """有到期 ``waiting`` 行的容器列表 —— worker drain tick 用。"""
        if now is None:
            now = int(time.time())
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT container FROM embed_backlog
                   WHERE status='waiting' AND next_retry_at<=?""",
                (now,),
            ).fetchall()
            return [r["container"] for r in rows]

    def counts(self, container: str) -> dict[str, int]:
        """某容器各状态计数：``{waiting, retrying, resolved, dead}``。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT status, COUNT(*) AS n FROM embed_backlog
                   WHERE container=? GROUP BY status""",
                (container,),
            ).fetchall()
        out = {s: 0 for s in BACKLOG_STATUSES}
        for r in rows:
            out[r["status"]] = int(r["n"])
        return out

    def summary(self, container: str) -> dict:
        """某容器 backlog 摘要：计数 + 最近错误类别 + 最近的 next_retry_at。"""
        counts = self.counts(container)
        with self._conn() as conn:
            row = conn.execute(
                """SELECT error_class, MIN(next_retry_at) AS soonest,
                          MAX(last_attempt_at) AS latest
                   FROM embed_backlog WHERE container=? AND status='waiting'""",
                (container,),
            ).fetchone()
        active = counts["waiting"] + counts["retrying"]
        return {
            "counts": counts,
            "active": active,
            "dead": counts["dead"],
            "next_retry_at": (
                int(row["soonest"]) if row and row["soonest"] is not None else None
            ),
            "last_error_class": row["error_class"] if row and active else None,
        }

    def list_items(
        self, container: str, status: str | None = None, limit: int = 100,
    ) -> list[BacklogItem]:
        sql = "SELECT * FROM embed_backlog WHERE container=?"
        params: list = [container]
        if status:
            if status not in BACKLOG_STATUSES:
                raise ValueError(f"invalid backlog status: {status}")
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY next_retry_at ASC, id ASC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            return [_row_to_item(r) for r in conn.execute(sql, params).fetchall()]

    def all_active_containers(self) -> list[str]:
        """有 waiting/retrying/dead 行的所有容器。"""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT DISTINCT container FROM embed_backlog
                   WHERE status IN ('waiting','retrying','dead')"""
            ).fetchall()
            return [r["container"] for r in rows]

    def purge_resolved(self, older_than_sec: int = 7 * 86400) -> int:
        """清掉过期的 ``resolved`` 行（``dead`` 行**永久保留**作 dead-letter）。"""
        cutoff = int(time.time()) - older_than_sec
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM embed_backlog WHERE status='resolved' AND resolved_at<?",
                (cutoff,),
            )
            return cur.rowcount
