"""Persistent SQLite-backed job queue for ingest workloads.

Design rationale (post-2026-04-30 incident, see docker-compose comments):
The server is write-heavy / read-rare — users dump many memories that may be
queried much later. Synchronous ingest pipelines stack subprocess load and
embedding API calls into bursts, which historically tipped the shared host
into swap-thrashing and D-state deadlock. The fix: accept writes immediately,
defer embedding into a single-worker queue that drains slowly, with
exponential backoff and coalescing, surviving restarts.

Schema:
    CREATE TABLE jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      op TEXT,                  -- 'embed', 'ingest-memory', 'ingest-structured'
      container TEXT,
      payload_json TEXT,        -- JSON: extra args for subprocess
      status TEXT,              -- 'pending', 'running', 'done', 'failed', 'cancelled'
      attempts INTEGER,
      max_attempts INTEGER,
      enqueued_at INTEGER,      -- unix ts
      next_run_at INTEGER,      -- earliest ts this job can be claimed
      started_at INTEGER,
      finished_at INTEGER,
      result_code INTEGER,
      last_error TEXT,
      pid INTEGER,
      label TEXT
    )

Coalescing:
    enqueue() checks if a (op, container) job is already pending. If so it
    returns the existing job_id rather than creating a duplicate. This keeps
    "post 100 memories then trigger 100 auto_embeds" from spawning 100 jobs.

Backoff schedule (seconds): 30, 60, 120, 300, 900. Capped at max_attempts=5.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("transcendence-memory-server.queue")


BACKOFF_SCHEDULE: tuple[int, ...] = (30, 60, 120, 300, 900)
DEFAULT_MAX_ATTEMPTS = len(BACKOFF_SCHEDULE)

VALID_STATUSES = {"pending", "running", "done", "failed", "cancelled"}


@dataclass
class Job:
    id: int
    op: str
    container: str
    payload: dict
    status: str
    attempts: int
    max_attempts: int
    enqueued_at: int
    next_run_at: int
    started_at: int | None
    finished_at: int | None
    result_code: int | None
    last_error: str | None
    pid: int | None
    label: str

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


class JobQueue:
    """SQLite-backed durable job queue. Thread-safe via per-instance lock.

    Connections are short-lived per call (sqlite3's threading model is finicky;
    short-lived connections are simplest and SQLite write throughput is plenty
    for this workload — we'll never exceed a few jobs/sec).
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # WAL gives us better concurrent read performance and crash safety
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
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    op TEXT NOT NULL,
                    container TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    enqueued_at INTEGER NOT NULL,
                    next_run_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    result_code INTEGER,
                    last_error TEXT,
                    pid INTEGER,
                    label TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_claim
                    ON jobs (status, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_dedup
                    ON jobs (op, container, status);
                """
            )
            # Recover from crash: any 'running' job at startup is presumed dead
            # (the worker that owned it is gone). Reset to pending so it gets
            # picked up again, with attempts incremented as a soft cap.
            now = int(time.time())
            cursor = conn.execute(
                """UPDATE jobs SET status='pending', next_run_at=?, pid=NULL,
                   last_error=COALESCE(last_error,'')||'\n[recovered after restart]'
                   WHERE status='running'""",
                (now,),
            )
            if cursor.rowcount:
                logger.warning("recovered %d running jobs after restart", cursor.rowcount)

    # ---------- enqueue ----------

    def enqueue(
        self,
        op: str,
        container: str,
        payload: dict | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        label: str = "",
        delay_sec: int = 0,
        coalesce: bool = True,
    ) -> int:
        """Add a job. If coalesce=True and a pending job for (op, container)
        already exists, returns its existing id without creating a duplicate.
        """
        now = int(time.time())
        next_run = now + max(0, delay_sec)
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        with self._conn() as conn:
            if coalesce:
                row = conn.execute(
                    """SELECT id FROM jobs
                       WHERE op=? AND container=? AND status='pending'
                       ORDER BY id ASC LIMIT 1""",
                    (op, container),
                ).fetchone()
                if row is not None:
                    # Refresh next_run_at if delayed less than already-scheduled
                    conn.execute(
                        """UPDATE jobs SET next_run_at=MIN(next_run_at, ?)
                           WHERE id=?""",
                        (next_run, row["id"]),
                    )
                    return int(row["id"])
            cursor = conn.execute(
                """INSERT INTO jobs (op, container, payload_json, status,
                       max_attempts, enqueued_at, next_run_at, label)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (op, container, payload_json, max_attempts, now, next_run, label),
            )
            return int(cursor.lastrowid or 0)

    # ---------- claim / mark ----------

    def claim_next(self, now: int | None = None) -> Job | None:
        """Atomically pick the oldest runnable job and mark it running."""
        if now is None:
            now = int(time.time())
        with self._conn() as conn:
            # SQLite supports BEGIN IMMEDIATE for short critical sections
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM jobs
                       WHERE status='pending' AND next_run_at <= ?
                       ORDER BY next_run_at ASC, id ASC LIMIT 1""",
                    (now,),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    """UPDATE jobs SET status='running', started_at=?,
                          attempts=attempts+1
                       WHERE id=?""",
                    (now, row["id"]),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            # Re-read to pick up updated attempts/started_at
            row2 = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return _row_to_job(row2)

    def attach_pid(self, job_id: int, pid: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE jobs SET pid=? WHERE id=?", (pid, job_id))

    def mark_done(self, job_id: int, result_code: int = 0, note: str | None = None) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute(
                """UPDATE jobs SET status='done', finished_at=?, result_code=?,
                      last_error=?, pid=NULL WHERE id=?""",
                (now, result_code, note, job_id),
            )

    def mark_failed(self, job_id: int, error: str) -> None:
        """Mark a single attempt as failed. If attempts < max_attempts the job
        is rescheduled with exponential backoff; otherwise it transitions to
        permanent 'failed' state.
        """
        now = int(time.time())
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            if attempts >= max_attempts:
                conn.execute(
                    """UPDATE jobs SET status='failed', finished_at=?,
                          last_error=?, pid=NULL WHERE id=?""",
                    (now, error, job_id),
                )
                return
            backoff_idx = min(attempts - 1, len(BACKOFF_SCHEDULE) - 1)
            backoff_idx = max(backoff_idx, 0)
            delay = BACKOFF_SCHEDULE[backoff_idx]
            conn.execute(
                """UPDATE jobs SET status='pending', next_run_at=?,
                      last_error=?, started_at=NULL, pid=NULL WHERE id=?""",
                (now + delay, error, job_id),
            )

    def cancel(self, job_id: int) -> bool:
        """Cancel a pending job. Returns True if the job was pending and is now
        cancelled. Running jobs cannot be cancelled (worker would need to be
        signalled separately)."""
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE jobs SET status='cancelled', finished_at=?
                   WHERE id=? AND status='pending'""",
                (int(time.time()), job_id),
            )
            return cursor.rowcount > 0

    # ---------- queries ----------

    def get(self, job_id: int) -> Job | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _row_to_job(row) if row else None

    def list_jobs(
        self,
        status: str | None = None,
        container: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: list = []
        if status:
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid status: {status}")
            sql += " AND status=?"
            params.append(status)
        if container:
            sql += " AND container=?"
            params.append(container)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            return [_row_to_job(r) for r in conn.execute(sql, params).fetchall()]

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        out = {s: 0 for s in VALID_STATUSES}
        for r in rows:
            out[r["status"]] = int(r["n"])
        return out

    def purge_done(self, older_than_sec: int = 7 * 86400) -> int:
        cutoff = int(time.time()) - older_than_sec
        with self._conn() as conn:
            cursor = conn.execute(
                """DELETE FROM jobs
                   WHERE status IN ('done','cancelled') AND finished_at < ?""",
                (cutoff,),
            )
            return cursor.rowcount


def _row_to_job(row: sqlite3.Row) -> Job:
    payload_raw = row["payload_json"] or "{}"
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        payload = {}
    return Job(
        id=int(row["id"]),
        op=row["op"],
        container=row["container"],
        payload=payload,
        status=row["status"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        enqueued_at=int(row["enqueued_at"]),
        next_run_at=int(row["next_run_at"]),
        started_at=int(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=int(row["finished_at"]) if row["finished_at"] is not None else None,
        result_code=int(row["result_code"]) if row["result_code"] is not None else None,
        last_error=row["last_error"],
        pid=int(row["pid"]) if row["pid"] is not None else None,
        label=row["label"] or "",
    )
