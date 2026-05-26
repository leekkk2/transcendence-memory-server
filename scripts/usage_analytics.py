"""Usage analytics middleware + admin endpoint handlers.

Records every authenticated HTTP request into a SQLite table
(``api_request_log``) for observability — endpoint call frequency, container
fan-out, error rate, latency distribution, distinct API key counts.

Design constraints (see ``docs/server-internal/plan/2026-05-26-usage-analytics-design.md``):

* Middleware **must not** block the response — every record goes through an
  in-memory queue drained by ``UsageBatcher.background_loop`` (≤ flush
  interval / batch size envs) so request latency overhead is bounded.
* ``path`` is normalised to the FastAPI route template (e.g.
  ``/containers/{name}/memories/{id}``) so cardinality stays bounded even
  if callers hit a thousand distinct containers.
* ``api_key`` is hashed (SHA-256, 16 hex chars) — never stored plain.
* All four admin endpoints rely on ``Depends(verify_auth)`` registered at
  the route level in ``task_rag_server.py``; the handlers in this module
  are pure functions that do not import the auth dependency themselves
  (avoids circular import with the server module).

The DB is the same ``queue.db`` already used by ``JobQueue``,
``BacklogStore``, ``IndexStateStore`` — single WAL, single backup domain.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger("transcendence-memory-server.usage")


# ---------------------------------------------------------------------------
# SQLite schema bootstrap
# ---------------------------------------------------------------------------

_SCHEMA_REQUEST_LOG = """
CREATE TABLE IF NOT EXISTS api_request_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  status INTEGER NOT NULL,
  latency_ms INTEGER NOT NULL,
  container TEXT,
  api_key_hash TEXT,
  ua TEXT,
  bytes_in INTEGER,
  bytes_out INTEGER,
  error_type TEXT
);
"""

_SCHEMA_DAILY_ROLLUP = """
CREATE TABLE IF NOT EXISTS daily_usage_rollup (
  day TEXT NOT NULL,
  path TEXT NOT NULL,
  calls INTEGER NOT NULL,
  errors INTEGER NOT NULL,
  p50_latency INTEGER NOT NULL,
  p95_latency INTEGER NOT NULL,
  p99_latency INTEGER NOT NULL,
  distinct_containers INTEGER NOT NULL,
  distinct_api_keys INTEGER NOT NULL,
  bytes_in_total INTEGER NOT NULL,
  bytes_out_total INTEGER NOT NULL,
  PRIMARY KEY (day, path)
);
"""

_SCHEMA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_log_ts ON api_request_log(ts DESC);",
    "CREATE INDEX IF NOT EXISTS idx_log_path_ts ON api_request_log(path, ts DESC);",
    (
        "CREATE INDEX IF NOT EXISTS idx_log_container_ts "
        "ON api_request_log(container, ts DESC) WHERE container IS NOT NULL;"
    ),
    "CREATE INDEX IF NOT EXISTS idx_rollup_day ON daily_usage_rollup(day DESC);",
]


def ensure_schema(db_path: str | Path) -> None:
    """Idempotently create the analytics tables / indexes."""
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_SCHEMA_REQUEST_LOG)
        conn.execute(_SCHEMA_DAILY_ROLLUP)
        for stmt in _SCHEMA_INDEXES:
            conn.execute(stmt)
        conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def hash_api_key(api_key: Optional[str]) -> Optional[str]:
    """SHA-256 of the api key, first 16 hex chars. Returns None for empty input."""
    if not api_key:
        return None
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def classify_error(status: int) -> str:
    """Coarse error class — feeds rollup ``errors`` counter + future alerts."""
    if status < 400:
        return "none"
    if status in (401, 403):
        return "auth"
    if status == 408 or status == 504:
        return "timeout"
    if status == 429:
        return "quota"
    if status >= 500:
        return "permanent"
    return "client"


_PATH_TEMPLATE_CACHE: dict[tuple[str, str], str] = {}


def normalised_path(request: Request) -> str:
    """Return the FastAPI route template path when available; fall back to
    the raw ``request.url.path`` (which still keeps cardinality low for
    static endpoints but explodes for path-parameterised ones, so the
    template path is strongly preferred).

    Starlette populates ``request.scope["route"]`` after routing succeeds —
    in middleware that runs **before** routing this is ``None``. We resolve
    via ``request.scope.get("route")`` post-``call_next`` so the value is
    available. For unmatched routes (404) we fall back to the raw path
    masked at depth 1 to avoid logging arbitrary user-supplied segments.
    """
    route = request.scope.get("route") if request.scope is not None else None
    if route is not None:
        path = getattr(route, "path", None)
        if path:
            return path
    raw = request.url.path or "/"
    # Unrouted 404 — keep just the first segment so we don't log unbounded
    # user-supplied strings as distinct paths.
    segs = raw.split("/", 2)
    if len(segs) >= 2 and segs[1]:
        return f"/{segs[1]}"
    return raw


def extract_container(request: Request, body_bytes: bytes | None, max_body: int) -> Optional[str]:
    """Best-effort container extraction.

    Lookup order: query string ``container`` param → JSON body ``container``
    key (only for POST/PUT and only if body size ≤ ``max_body``).
    Returns ``None`` if no signal — keeps the row anonymous rather than
    storing a random heuristic.
    """
    try:
        qval = request.query_params.get("container")
        if qval:
            return qval[:120]
    except Exception:  # pragma: no cover - defensive
        pass
    if not body_bytes or request.method not in ("POST", "PUT", "PATCH"):
        return None
    if len(body_bytes) > max_body:
        return None
    try:
        parsed = json.loads(body_bytes.decode("utf-8", errors="ignore"))
    except Exception:
        return None
    if isinstance(parsed, dict):
        val = parsed.get("container")
        if isinstance(val, str) and val:
            return val[:120]
    return None


# ---------------------------------------------------------------------------
# Batched writer
# ---------------------------------------------------------------------------


class UsageBatcher:
    """Coalesce middleware records and flush to SQLite on a steady cadence.

    The middleware pushes records into a bounded ``asyncio.Queue``. A
    background task drains the queue every ``flush_interval_ms`` (or sooner
    if it reaches ``flush_batch_size``) and writes them with
    ``executemany`` so the per-request hot path never blocks on disk I/O.

    Failure isolation: if the writer raises (disk full, schema mismatch)
    we log + count failures; after ``_max_consecutive_failures`` the
    batcher self-disables to stop tail-spinning.
    """

    _MAX_QUEUE = 10_000
    _MAX_CONSECUTIVE_FAILURES = 10

    def __init__(
        self,
        db_path: str | Path,
        flush_interval_ms: int = 10,
        flush_batch_size: int = 100,
    ) -> None:
        self.db_path = str(db_path)
        self.flush_interval_ms = max(1, flush_interval_ms)
        self.flush_batch_size = max(1, flush_batch_size)
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._MAX_QUEUE)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._consecutive_failures = 0
        self._disabled = False
        self._dropped = 0

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def dropped(self) -> int:
        return self._dropped

    def add(self, record: dict[str, Any]) -> None:
        if self._disabled:
            return
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped += 1
            # One log per batch worth of drops so we don't flood.
            if self._dropped % self.flush_batch_size == 1:
                logger.warning(
                    "usage batcher queue full; dropped %d records so far",
                    self._dropped,
                )

    def start(self) -> None:
        if self._task is not None or self._disabled:
            return
        self._stop.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("usage batcher start called outside event loop; deferring")
            return
        self._task = loop.create_task(self.background_loop(), name="tm-usage-batcher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        await self._flush_remaining()

    async def background_loop(self) -> None:
        interval = self.flush_interval_ms / 1000.0
        while not self._stop.is_set():
            try:
                await asyncio.sleep(interval)
                if self._queue.qsize() == 0:
                    continue
                await self.flush()
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("usage batcher loop error: %s", exc)

    async def flush(self) -> None:
        if self._queue.empty():
            return
        batch: list[dict[str, Any]] = []
        while not self._queue.empty() and len(batch) < self.flush_batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:  # pragma: no cover - race
                break
        if not batch:
            return
        # SQLite write off the event loop thread.
        await asyncio.to_thread(self._write_batch, batch)

    async def _flush_remaining(self) -> None:
        # Final drain on shutdown — may make several writes if backlog > batch.
        while not self._queue.empty():
            await self.flush()

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        try:
            with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.executemany(
                    """
                    INSERT INTO api_request_log
                        (ts, method, path, status, latency_ms, container,
                         api_key_hash, ua, bytes_in, bytes_out, error_type)
                    VALUES
                        (:ts, :method, :path, :status, :latency_ms, :container,
                         :api_key_hash, :ua, :bytes_in, :bytes_out, :error_type)
                    """,
                    batch,
                )
                conn.commit()
            self._consecutive_failures = 0
        except Exception as exc:
            self._consecutive_failures += 1
            logger.warning(
                "usage batcher write failed (%d/%d): %s",
                self._consecutive_failures,
                self._MAX_CONSECUTIVE_FAILURES,
                exc,
            )
            if self._consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                self._disabled = True
                logger.error(
                    "usage batcher disabled after %d consecutive failures",
                    self._consecutive_failures,
                )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class UsageMiddleware(BaseHTTPMiddleware):
    """Record one row per HTTP request via ``UsageBatcher``.

    Implemented as a ``BaseHTTPMiddleware`` so we get easy access to the
    matched route in ``request.scope["route"]`` after ``call_next``. The
    overhead is one ``time.monotonic()`` pair + a dict push into an
    in-memory queue — measured well below 1 ms on the local dev box.
    """

    # Paths we never log (avoids log echo: health checks → noisy rows that
    # dominate ``calls`` and skew rollups).
    _IGNORED_PATHS = {"/health", "/metrics", "/favicon.ico"}

    def __init__(
        self,
        app: ASGIApp,
        db_path: str | Path,
        *,
        enabled: bool = True,
        batcher: UsageBatcher | None = None,
        log_ua: bool = True,
        max_body_inspect: int = 1_048_576,
    ) -> None:
        super().__init__(app)
        self.db_path = str(db_path)
        self.enabled = enabled
        self.log_ua = log_ua
        self.max_body_inspect = max_body_inspect
        self.batcher = batcher
        if self.enabled and self.batcher is None:
            ensure_schema(self.db_path)
            self.batcher = UsageBatcher(
                self.db_path,
                flush_interval_ms=_env_int("TM_USAGE_FLUSH_INTERVAL_MS", 10),
                flush_batch_size=_env_int("TM_USAGE_FLUSH_BATCH_SIZE", 100),
            )

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if not self.enabled or self.batcher is None or self.batcher.disabled:
            return await call_next(request)
        if request.url.path in self._IGNORED_PATHS:
            return await call_next(request)

        # Lazy-start the batcher task on the first request — by then the
        # event loop is definitely running.
        if self.batcher._task is None:
            self.batcher.start()

        # Capture body for container extraction (only for POST/PUT/PATCH;
        # we always re-inject for downstream handlers via receive override).
        body_bytes: bytes | None = None
        if request.method in ("POST", "PUT", "PATCH"):
            try:
                body_bytes = await request.body()
            except Exception:
                body_bytes = None

            if body_bytes is not None:
                # Re-inject the consumed body so route handlers can read it.
                async def receive():
                    return {"type": "http.request", "body": body_bytes, "more_body": False}

                request._receive = receive  # type: ignore[attr-defined]

        api_key = request.headers.get("x-api-key")
        if not api_key:
            authz = request.headers.get("authorization") or ""
            if authz.lower().startswith("bearer "):
                api_key = authz.split(" ", 1)[1]

        ua = (request.headers.get("user-agent") or "")[:80] if self.log_ua else None
        bytes_in = len(body_bytes) if body_bytes is not None else (
            int(request.headers.get("content-length") or 0)
        )

        start = time.monotonic()
        status = 500
        bytes_out = 0
        response: Response | None = None
        try:
            response = await call_next(request)
            status = response.status_code
            cl = response.headers.get("content-length")
            if cl is not None:
                try:
                    bytes_out = int(cl)
                except ValueError:
                    bytes_out = 0
            return response
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            try:
                container = extract_container(request, body_bytes, self.max_body_inspect)
                record = {
                    "ts": int(time.time() * 1000),
                    "method": request.method,
                    "path": normalised_path(request),
                    "status": int(status),
                    "latency_ms": latency_ms,
                    "container": container,
                    "api_key_hash": hash_api_key(api_key),
                    "ua": ua,
                    "bytes_in": int(bytes_in or 0),
                    "bytes_out": int(bytes_out or 0),
                    "error_type": classify_error(int(status)),
                }
                self.batcher.add(record)
            except Exception as exc:  # pragma: no cover - never block response
                logger.warning("usage middleware record failed: %s", exc)


# ---------------------------------------------------------------------------
# Query helpers (shared by handlers)
# ---------------------------------------------------------------------------


_WINDOW_SECONDS = {
    "1h": 3_600,
    "24h": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
}


def _window_seconds(window: str) -> int:
    try:
        return _WINDOW_SECONDS[window]
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid window {window!r}; allowed: {sorted(_WINDOW_SECONDS)}",
        ) from exc


def _connect(db_path: str | Path) -> sqlite3.Connection:
    ensure_schema(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _percentile(values: list[int], pct: float) -> int:
    """In-Python percentile — small windows so this is cheap. Returns 0 for
    empty input so the JSON response stays well-typed."""
    if not values:
        return 0
    values = sorted(values)
    k = max(0, min(len(values) - 1, int(round((pct / 100.0) * (len(values) - 1)))))
    return int(values[k])


# ---------------------------------------------------------------------------
# Admin handlers
# ---------------------------------------------------------------------------


def summary(db_path: str | Path, window: str = "24h") -> dict[str, Any]:
    """Aggregate counters + latency for the window."""
    seconds = _window_seconds(window)
    cutoff_ms = int(time.time() * 1000) - seconds * 1000
    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT status, latency_ms, path, container, api_key_hash "
            "FROM api_request_log WHERE ts >= ?",
            (cutoff_ms,),
        ).fetchall()

    total_calls = len(rows)
    total_errors = sum(1 for r in rows if int(r["status"]) >= 400)
    latencies = [int(r["latency_ms"]) for r in rows]
    containers = {r["container"] for r in rows if r["container"]}
    api_keys = {r["api_key_hash"] for r in rows if r["api_key_hash"]}

    per_path: dict[str, list[int]] = {}
    per_path_calls: dict[str, int] = {}
    for r in rows:
        p = r["path"]
        per_path.setdefault(p, []).append(int(r["latency_ms"]))
        per_path_calls[p] = per_path_calls.get(p, 0) + 1

    top = sorted(per_path_calls.items(), key=lambda kv: kv[1], reverse=True)[:10]
    top_endpoints = [
        {
            "path": path,
            "calls": calls,
            "p95": _percentile(per_path[path], 95),
        }
        for path, calls in top
    ]

    return {
        "window": window,
        "total_calls": total_calls,
        "total_errors": total_errors,
        "error_rate": (total_errors / total_calls) if total_calls else 0.0,
        "p50_latency_ms": _percentile(latencies, 50),
        "p95_latency_ms": _percentile(latencies, 95),
        "active_containers": len(containers),
        "active_api_keys": len(api_keys),
        "top_endpoints": top_endpoints,
    }


SortBy = Literal["calls", "errors", "p95"]


def endpoints(
    db_path: str | Path,
    window: str = "7d",
    sort: SortBy = "calls",
    limit: int = 20,
) -> dict[str, Any]:
    """Endpoint-level ranking + cold endpoint detection."""
    if sort not in ("calls", "errors", "p95"):
        raise HTTPException(status_code=400, detail=f"invalid sort {sort!r}")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be in [1, 200]")

    seconds = _window_seconds(window)
    cutoff_ms = int(time.time() * 1000) - seconds * 1000
    cold_cutoff_ms = int(time.time() * 1000) - 30 * 86_400 * 1000

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT path, status, latency_ms, container, ts "
            "FROM api_request_log WHERE ts >= ?",
            (cutoff_ms,),
        ).fetchall()

        cold_rows = conn.execute(
            "SELECT path, COUNT(*) AS calls, MAX(ts) AS last_called_at "
            "FROM api_request_log WHERE ts >= ? GROUP BY path",
            (cold_cutoff_ms,),
        ).fetchall()

    per_path: dict[str, dict[str, Any]] = {}
    per_path_latencies: dict[str, list[int]] = {}
    per_path_containers: dict[str, set[str]] = {}
    for r in rows:
        p = r["path"]
        d = per_path.setdefault(
            p,
            {
                "path": p,
                "calls": 0,
                "errors": 0,
                "last_called_at": 0,
            },
        )
        d["calls"] += 1
        if int(r["status"]) >= 400:
            d["errors"] += 1
        d["last_called_at"] = max(d["last_called_at"], int(r["ts"]))
        per_path_latencies.setdefault(p, []).append(int(r["latency_ms"]))
        if r["container"]:
            per_path_containers.setdefault(p, set()).add(r["container"])

    enriched = []
    for path, d in per_path.items():
        latencies = per_path_latencies.get(path, [])
        enriched.append(
            {
                **d,
                "p50_latency_ms": _percentile(latencies, 50),
                "p95_latency_ms": _percentile(latencies, 95),
                "distinct_containers": len(per_path_containers.get(path, set())),
            }
        )

    key_fn = {
        "calls": lambda x: x["calls"],
        "errors": lambda x: x["errors"],
        "p95": lambda x: x["p95_latency_ms"],
    }[sort]
    enriched.sort(key=key_fn, reverse=True)
    enriched = enriched[:limit]

    cold_endpoints = []
    for r in cold_rows:
        calls = int(r["calls"])
        if calls < 10:
            cold_endpoints.append(
                {
                    "path": r["path"],
                    "calls": calls,
                    "last_called_at": int(r["last_called_at"]) if r["last_called_at"] else None,
                }
            )

    return {
        "window": window,
        "sort": sort,
        "rows": enriched,
        "cold_endpoints": cold_endpoints,
    }


def containers(
    db_path: str | Path,
    window: str = "7d",
    sort: Literal["calls"] = "calls",
    limit: int = 50,
) -> dict[str, Any]:
    """Container-dimension fan-out."""
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be in [1, 500]")

    seconds = _window_seconds(window)
    cutoff_ms = int(time.time() * 1000) - seconds * 1000

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT container, path, ts FROM api_request_log "
            "WHERE ts >= ? AND container IS NOT NULL",
            (cutoff_ms,),
        ).fetchall()

    per_container: dict[str, dict[str, Any]] = {}
    for r in rows:
        c = r["container"]
        d = per_container.setdefault(
            c,
            {
                "container": c,
                "calls": 0,
                "search_calls": 0,
                "ingest_calls": 0,
                "embed_calls": 0,
                "last_active": 0,
            },
        )
        d["calls"] += 1
        path = r["path"] or ""
        if path.startswith("/search"):
            d["search_calls"] += 1
        elif path.startswith("/ingest") or "/memories" in path:
            d["ingest_calls"] += 1
        elif path.startswith("/embed"):
            d["embed_calls"] += 1
        d["last_active"] = max(d["last_active"], int(r["ts"]))

    enriched = sorted(per_container.values(), key=lambda x: x["calls"], reverse=True)[:limit]

    idle_containers: list[dict[str, Any]] = []
    return {
        "window": window,
        "rows": enriched,
        "idle_containers": idle_containers,
    }


def timeseries(
    db_path: str | Path,
    path: str,
    window: str = "7d",
    bucket: Literal["5m", "1h", "1d"] = "1h",
) -> dict[str, Any]:
    """Bucketed time series for a single endpoint."""
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    seconds = _window_seconds(window)

    bucket_seconds = {"5m": 300, "1h": 3600, "1d": 86_400}.get(bucket)
    if bucket_seconds is None:
        raise HTTPException(status_code=400, detail=f"invalid bucket {bucket!r}")

    # Force coarser buckets for long windows to bound row count.
    if seconds > 7 * 86_400 and bucket_seconds < 3600:
        bucket_seconds = 3600
        bucket = "1h"  # type: ignore[assignment]
    if seconds > 30 * 86_400 and bucket_seconds < 86_400:
        bucket_seconds = 86_400
        bucket = "1d"  # type: ignore[assignment]

    cutoff_ms = int(time.time() * 1000) - seconds * 1000
    bucket_ms = bucket_seconds * 1000

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT ts, status, latency_ms FROM api_request_log "
            "WHERE path = ? AND ts >= ? ORDER BY ts ASC",
            (path, cutoff_ms),
        ).fetchall()

    buckets: dict[int, dict[str, Any]] = {}
    for r in rows:
        ts = int(r["ts"])
        b = (ts // bucket_ms) * bucket_ms
        d = buckets.setdefault(b, {"ts": b, "calls": 0, "errors": 0, "latencies": []})
        d["calls"] += 1
        if int(r["status"]) >= 400:
            d["errors"] += 1
        d["latencies"].append(int(r["latency_ms"]))

    points = []
    for b in sorted(buckets):
        d = buckets[b]
        points.append(
            {
                "ts": d["ts"],
                "calls": d["calls"],
                "errors": d["errors"],
                "p95": _percentile(d["latencies"], 95),
            }
        )

    return {
        "path": path,
        "window": window,
        "bucket": bucket,
        "points": points,
    }


def cleanup(db_path: str | Path, retention_days: int = 30) -> dict[str, int]:
    """Drop ``api_request_log`` rows older than ``retention_days``."""
    if retention_days < 1 or retention_days > 3650:
        raise HTTPException(status_code=400, detail="retention_days must be in [1, 3650]")
    cutoff_ms = int(time.time() * 1000) - retention_days * 86_400 * 1000
    with closing(_connect(db_path)) as conn:
        before = conn.execute("SELECT COUNT(*) FROM api_request_log").fetchone()[0]
        conn.execute("DELETE FROM api_request_log WHERE ts < ?", (cutoff_ms,))
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM api_request_log").fetchone()[0]
    return {"deleted_rows": int(before - after), "kept_rows": int(after)}


# ---------------------------------------------------------------------------
# Daily rollup (called by job_worker on a cadence)
# ---------------------------------------------------------------------------


def rollup_day(db_path: str | Path, day: Optional[str] = None) -> dict[str, Any]:
    """Compute the ``daily_usage_rollup`` row for the given UTC day.

    ``day`` defaults to *yesterday* UTC (the typical cron contract:
    finalise the previous day's totals once today has started). Pass an
    explicit ``YYYY-MM-DD`` for backfill.
    """
    ensure_schema(db_path)
    if day is None:
        now = datetime.now(timezone.utc)
        # Yesterday — rollup runs once today is underway.
        day_obj = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        day_obj = day_obj.fromtimestamp(day_obj.timestamp() - 86_400, tz=timezone.utc)
        day = day_obj.strftime("%Y-%m-%d")

    try:
        day_obj = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid day {day!r}") from exc

    start_ms = int(day_obj.timestamp() * 1000)
    end_ms = start_ms + 86_400_000

    with closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT path, status, latency_ms, container, api_key_hash, bytes_in, bytes_out "
            "FROM api_request_log WHERE ts >= ? AND ts < ?",
            (start_ms, end_ms),
        ).fetchall()

        per_path: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = per_path.setdefault(
                r["path"],
                {
                    "calls": 0,
                    "errors": 0,
                    "latencies": [],
                    "containers": set(),
                    "api_keys": set(),
                    "bytes_in_total": 0,
                    "bytes_out_total": 0,
                },
            )
            d["calls"] += 1
            if int(r["status"]) >= 400:
                d["errors"] += 1
            d["latencies"].append(int(r["latency_ms"]))
            if r["container"]:
                d["containers"].add(r["container"])
            if r["api_key_hash"]:
                d["api_keys"].add(r["api_key_hash"])
            d["bytes_in_total"] += int(r["bytes_in"] or 0)
            d["bytes_out_total"] += int(r["bytes_out"] or 0)

        upserts = []
        for path, d in per_path.items():
            upserts.append(
                (
                    day,
                    path,
                    d["calls"],
                    d["errors"],
                    _percentile(d["latencies"], 50),
                    _percentile(d["latencies"], 95),
                    _percentile(d["latencies"], 99),
                    len(d["containers"]),
                    len(d["api_keys"]),
                    d["bytes_in_total"],
                    d["bytes_out_total"],
                )
            )

        if upserts:
            conn.executemany(
                """
                INSERT OR REPLACE INTO daily_usage_rollup
                    (day, path, calls, errors, p50_latency, p95_latency, p99_latency,
                     distinct_containers, distinct_api_keys, bytes_in_total, bytes_out_total)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                upserts,
            )
            conn.commit()

    return {"day": day, "paths_rolled_up": len(per_path)}
