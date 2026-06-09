#!/usr/bin/env python3
"""Global token metering + quota circuit breaker (blueprint P3, §7).

This module is the LLM-gateway side-car that turns every model call's `usage`
metadata into (a) real-time multi-dimensional Redis counters and (b) an
async-batched SQLite rollup for the future cost dashboard, and exposes an
opt-in quota gate that — only when a founder has configured a budget — flips a
breaker and routes over-budget agents to a cheaper fallback model.

Three invariants govern everything here (mirror redis_client.py P0 /
config_store.py P1):

  * **Fire-and-forget, never blocking.** record_usage runs entirely inside a
    try/except; a Redis or DB hiccup degrades to a debug log and the caller's
    LLM path is untouched. It NEVER raises into call_openai_chat.
  * **Fail-open quota.** over_budget reads the budget from config_store
    (default None) and the live count from Redis. With NO budget configured it
    returns over=False unconditionally — so the default deployment behaves
    byte-identically to pre-P3 (the gate is a no-op). When Redis is down it
    ALSO returns over=False (treat a metering outage as "let the request
    through", governance failure must not misfire and kill an agent).
  * **Behavior-preserving by default.** No budget override → no enforcement →
    no model downgrade → the LLM call is exactly what it was before P3.

Redis key templates (all under the `usage:tokens:*` / `circuit:token:*`
namespaces; counters TTL to the end of their window so stale buckets evict):

    usage:tokens:daily:{YYYYMMDD}:{model}:{task_type}:{agent_id}   (hash)
    usage:tokens:hourly:{YYYYMMDDHH}:{model}:{task_type}:{agent_id} (hash)
    circuit:token:{agent_id}                                       (string marker)

Each counter hash carries three fields: prompt / completion / total.

R8: pure generic code — no private endpoint / hostname / credential / private
container name. The fallback model is read from config (a placeholder by
default); HR-9 routing through the sanctioned gateway is enforced by the caller
(model_fallback.run_with_fallback over the configured profiles), not here.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import redis_client  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import redis_client  # type: ignore

try:
    import config_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import config_store  # type: ignore

logger = logging.getLogger("transcendence-memory-server.token_meter")


# ---------------------------------------------------------------------------
# Context propagation: task_type + agent_id
# ---------------------------------------------------------------------------
# call_openai_chat runs deep inside LightRAG's llm_model_func, far from the
# request handler that knows which agent / task this is. A ContextVar carries
# that context down the async call chain without threading it through every
# signature. Defaults keep behavior sane when nothing was set (e.g. a worker
# subprocess or a direct module-level call).

DEFAULT_TASK_TYPE = "rag_retrieval"
DEFAULT_AGENT_ID = "unknown"

_task_type_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tm_task_type", default=DEFAULT_TASK_TYPE
)
_agent_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tm_agent_id", default=DEFAULT_AGENT_ID
)


def set_task_type(task_type: Optional[str]) -> contextvars.Token:
    """Bind the current task_type for downstream record_usage. Returns the
    reset token (caller may reset in a finally, but leaking is harmless since
    each request runs its own asyncio Task with an isolated context copy)."""
    return _task_type_var.set((task_type or DEFAULT_TASK_TYPE).strip() or DEFAULT_TASK_TYPE)


def get_task_type() -> str:
    return _task_type_var.get()


def set_agent_id(agent_id: Optional[str]) -> contextvars.Token:
    """Bind the current agent_id for downstream record_usage."""
    return _agent_id_var.set((agent_id or DEFAULT_AGENT_ID).strip() or DEFAULT_AGENT_ID)


def get_agent_id() -> str:
    return _agent_id_var.get()


def resolve_agent_id(
    header_agent_id: Optional[str], api_key_hash: Optional[str]
) -> str:
    """Pick the agent identity for a request.

    Priority: explicit ``X-Agent-ID`` header → the hashed api key (so distinct
    keys are distinct agents without leaking the plaintext) → DEFAULT_AGENT_ID.
    Pure function; the caller binds the result via set_agent_id.
    """
    if header_agent_id and header_agent_id.strip():
        return header_agent_id.strip()[:120]
    if api_key_hash and api_key_hash.strip():
        return api_key_hash.strip()[:120]
    return DEFAULT_AGENT_ID


# ---------------------------------------------------------------------------
# Redis key + window helpers
# ---------------------------------------------------------------------------

_DAILY_PREFIX = "usage:tokens:daily"
_HOURLY_PREFIX = "usage:tokens:hourly"
_BREAKER_PREFIX = "circuit:token"


def _sanitize(part: str) -> str:
    """Keep Redis keys flat: collapse the `:` delimiter inside a component so a
    model/task/agent name with a colon can't shift the key layout."""
    return (part or "").replace(":", "_")


def _daily_key(model: str, task_type: str, agent_id: str, now: datetime) -> str:
    return (
        f"{_DAILY_PREFIX}:{now.strftime('%Y%m%d')}:"
        f"{_sanitize(model)}:{_sanitize(task_type)}:{_sanitize(agent_id)}"
    )


def _hourly_key(model: str, task_type: str, agent_id: str, now: datetime) -> str:
    return (
        f"{_HOURLY_PREFIX}:{now.strftime('%Y%m%d%H')}:"
        f"{_sanitize(model)}:{_sanitize(task_type)}:{_sanitize(agent_id)}"
    )


def _breaker_key(agent_id: str) -> str:
    return f"{_BREAKER_PREFIX}:{_sanitize(agent_id)}"


def _end_of_day_ts(now: datetime) -> int:
    """Unix ts at the next UTC midnight (the daily bucket's TTL anchor)."""
    midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return int(midnight.timestamp()) + 86_400


def _end_of_hour_ts(now: datetime) -> int:
    """Unix ts at the top of the next hour (the hourly bucket's TTL anchor)."""
    top = datetime(now.year, now.month, now.day, now.hour, tzinfo=timezone.utc)
    return int(top.timestamp()) + 3_600


# ---------------------------------------------------------------------------
# Batched SQLite rollup writer (mirrors usage_analytics.UsageBatcher)
# ---------------------------------------------------------------------------

_SCHEMA_TOKEN_ROLLUP = """
CREATE TABLE IF NOT EXISTS token_usage_rollup (
  day               TEXT NOT NULL,
  model             TEXT NOT NULL,
  task_type         TEXT NOT NULL,
  agent_id          TEXT NOT NULL,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, model, task_type, agent_id)
);
"""

_SCHEMA_TOKEN_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_token_rollup_day ON token_usage_rollup(day DESC);",
    "CREATE INDEX IF NOT EXISTS idx_token_rollup_model ON token_usage_rollup(model);",
    "CREATE INDEX IF NOT EXISTS idx_token_rollup_agent ON token_usage_rollup(agent_id);",
]


def ensure_schema(db_path: str | Path) -> None:
    """Idempotently create the token_usage_rollup table / indexes."""
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_SCHEMA_TOKEN_ROLLUP)
        for stmt in _SCHEMA_TOKEN_INDEXES:
            conn.execute(stmt)
        conn.commit()


class TokenBatcher:
    """Coalesce per-call token records and UPSERT-accumulate them into
    ``token_usage_rollup`` on a steady cadence — the SQLite half of metering.

    Same shape as ``usage_analytics.UsageBatcher``: a bounded asyncio.Queue
    drained by a background task every ``flush_interval`` seconds (or sooner
    when it reaches ``flush_batch_size``); writes go off the event loop via
    ``asyncio.to_thread`` so the hot path never blocks on disk I/O. After
    ``_MAX_CONSECUTIVE_FAILURES`` it self-disables to stop tail-spinning.

    Unlike the request-log batcher this UPSERTs (accumulates) into a rollup
    keyed by (day, model, task_type, agent_id), so the table stays small even
    under heavy traffic.
    """

    _MAX_QUEUE = 10_000
    _MAX_CONSECUTIVE_FAILURES = 10

    def __init__(
        self,
        db_path: str | Path,
        flush_interval: float = 60.0,
        flush_batch_size: int = 500,
    ) -> None:
        self.db_path = str(db_path)
        self.flush_interval = max(1.0, float(flush_interval))
        self.flush_batch_size = max(1, int(flush_batch_size))
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
            if self._dropped % self.flush_batch_size == 1:
                logger.warning(
                    "token batcher queue full; dropped %d records so far",
                    self._dropped,
                )

    def start(self) -> None:
        if self._task is not None or self._disabled:
            return
        self._stop.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("token batcher start called outside event loop; deferring")
            return
        self._task = loop.create_task(self.background_loop(), name="tm-token-batcher")

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
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self.flush_interval)
                if self._queue.qsize() == 0:
                    continue
                await self.flush()
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                break
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("token batcher loop error: %s", exc)

    async def flush(self) -> None:
        if self._queue.empty():
            return
        # Coalesce the whole pending backlog (bounded by queue cap) and
        # pre-aggregate in-process so the UPSERT touches one row per key.
        batch: list[dict[str, Any]] = []
        while not self._queue.empty():
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:  # pragma: no cover - race
                break
        if not batch:
            return
        await asyncio.to_thread(self._write_batch, batch)

    async def _flush_remaining(self) -> None:
        while not self._queue.empty():
            await self.flush()

    def _aggregate(self, batch: list[dict[str, Any]]) -> list[tuple]:
        """Pre-sum records sharing a (day, model, task_type, agent_id) key so a
        single flush issues one UPSERT per distinct key instead of N."""
        acc: dict[tuple[str, str, str, str], list[int]] = {}
        for r in batch:
            key = (
                str(r.get("day", "")),
                str(r.get("model", "")),
                str(r.get("task_type", "")),
                str(r.get("agent_id", "")),
            )
            slot = acc.setdefault(key, [0, 0, 0])
            slot[0] += int(r.get("prompt_tokens", 0) or 0)
            slot[1] += int(r.get("completion_tokens", 0) or 0)
            slot[2] += int(r.get("total_tokens", 0) or 0)
        return [(*k, v[0], v[1], v[2]) for k, v in acc.items()]

    def _write_batch(self, batch: list[dict[str, Any]]) -> None:
        rows = self._aggregate(batch)
        if not rows:
            return
        try:
            with closing(sqlite3.connect(self.db_path, timeout=5.0)) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                # UPSERT accumulate — existing row's counts grow by this flush.
                conn.executemany(
                    """
                    INSERT INTO token_usage_rollup
                        (day, model, task_type, agent_id,
                         prompt_tokens, completion_tokens, total_tokens)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(day, model, task_type, agent_id) DO UPDATE SET
                        prompt_tokens     = prompt_tokens     + excluded.prompt_tokens,
                        completion_tokens = completion_tokens + excluded.completion_tokens,
                        total_tokens      = total_tokens      + excluded.total_tokens
                    """,
                    rows,
                )
                conn.commit()
            self._consecutive_failures = 0
        except Exception as exc:
            self._consecutive_failures += 1
            logger.warning(
                "token batcher write failed (%d/%d): %s",
                self._consecutive_failures,
                self._MAX_CONSECUTIVE_FAILURES,
                exc,
            )
            if self._consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                self._disabled = True
                logger.error(
                    "token batcher disabled after %d consecutive failures",
                    self._consecutive_failures,
                )


# Process-level singleton batcher — lifecycle owned by the server lifespan.
_BATCHER: Optional[TokenBatcher] = None


def init_batcher(db_path: str | Path) -> TokenBatcher:
    """Build + start the process-level TokenBatcher. Idempotent; safe in
    lifespan startup. ensure_schema runs here so the table exists before the
    first flush. Never raises (degrades to a disabled batcher)."""
    global _BATCHER
    if _BATCHER is not None:
        return _BATCHER
    try:
        ensure_schema(db_path)
    except Exception as exc:  # noqa: BLE001 - never break boot on schema init
        logger.warning("token batcher schema init failed: %s", exc)
    flush_interval = _flush_interval_seconds()
    _BATCHER = TokenBatcher(db_path, flush_interval=flush_interval)
    _BATCHER.start()
    return _BATCHER


async def shutdown_batcher() -> None:
    """Flush + stop the batcher. Safe in lifespan shutdown; never raises."""
    global _BATCHER
    batcher = _BATCHER
    _BATCHER = None
    if batcher is None:
        return
    try:
        await batcher.stop()
    except Exception as exc:  # noqa: BLE001 - shutdown must not raise
        logger.warning("token batcher shutdown error: %s", exc)


def _flush_interval_seconds() -> float:
    """config:token:flush_interval (default 60s). Degrades to 60 on bad value."""
    try:
        val = config_store.get_cached("config:token:flush_interval", 60)
        return max(1.0, float(val))
    except Exception:  # noqa: BLE001 - never let config break the batcher
        return 60.0


# ---------------------------------------------------------------------------
# record_usage — fire-and-forget capture (Redis counters + SQLite enqueue)
# ---------------------------------------------------------------------------


async def record_usage(
    model: str,
    task_type: str,
    agent_id: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record one LLM call's token usage. Fire-and-forget, never blocks/raises.

    Side effects (all best-effort, each independently degrade-safe):
      1. Redis HINCRBY the daily + hourly hashes (prompt/completion/total) and
         EXPIREAT each to the end of its window.
      2. Enqueue a record into the TokenBatcher for SQLite rollup.

    A zero-token call (no usage surfaced) is skipped entirely — nothing to
    count. The WHOLE body is wrapped so a Redis/DB/asyncio failure only emits a
    debug log; it can never bubble into the LLM gateway.
    """
    try:
        prompt = max(0, int(prompt_tokens or 0))
        completion = max(0, int(completion_tokens or 0))
        total = prompt + completion
        if total <= 0:
            return
        model = model or "unknown"
        task_type = task_type or DEFAULT_TASK_TYPE
        agent_id = agent_id or DEFAULT_AGENT_ID
        now = datetime.now(timezone.utc)

        # 1) Redis real-time counters (both windows). Each helper degrades to a
        #    no-op when Redis is down — a None return just means "not counted".
        #    Two key shapes per window:
        #      - fine-grained per-(model,task,agent) hash → cost dashboard slices
        #      - per-agent aggregate hash (__agent__) → O(1) over_budget read
        #        (avoids a KEYS glob scan in prod). Both share the window TTL.
        daily_stamp = now.strftime("%Y%m%d")
        hourly_stamp = now.strftime("%Y%m%d%H")
        windows = (
            (
                _daily_key(model, task_type, agent_id, now),
                _agent_total_key(_DAILY_PREFIX, daily_stamp, agent_id),
                _end_of_day_ts(now),
            ),
            (
                _hourly_key(model, task_type, agent_id, now),
                _agent_total_key(_HOURLY_PREFIX, hourly_stamp, agent_id),
                _end_of_hour_ts(now),
            ),
        )
        for fine_key, agg_key, ttl_ts in windows:
            for key in (fine_key, agg_key):
                new_total = await redis_client.hincr(key, "total", total)
                if prompt:
                    await redis_client.hincr(key, "prompt", prompt)
                if completion:
                    await redis_client.hincr(key, "completion", completion)
                # Pin TTL once (cheap to re-set; EXPIREAT is idempotent).
                if new_total is not None:
                    await redis_client.expire_at(key, ttl_ts)

        # 2) SQLite rollup enqueue (async-batched). No-op if batcher absent.
        batcher = _BATCHER
        if batcher is not None:
            batcher.add(
                {
                    "day": now.strftime("%Y-%m-%d"),
                    "model": model,
                    "task_type": task_type,
                    "agent_id": agent_id,
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": total,
                }
            )
    except Exception as exc:  # noqa: BLE001 - metering must never break the call
        logger.debug("record_usage swallowed error: %s", exc)


# ---------------------------------------------------------------------------
# over_budget — opt-in quota gate (fail-open, default OFF)
# ---------------------------------------------------------------------------


def _read_budget(key: str) -> Optional[int]:
    """Read a token budget from config (default None = no limit). A None / 0 /
    negative value all mean "unlimited" (enforcement off). Never raises."""
    try:
        val = config_store.get_cached(key, None)
    except Exception:  # noqa: BLE001 - config failure → treat as no budget
        return None
    if val is None:
        return None
    try:
        budget = int(val)
    except (TypeError, ValueError):
        return None
    return budget if budget > 0 else None


def _fallback_model() -> Optional[str]:
    """config:token:fallback_model, or None when unset/empty. Never raises."""
    try:
        val = config_store.get_cached("config:token:fallback_model", None)
    except Exception:  # noqa: BLE001
        return None
    if val is None:
        return None
    s = str(val).strip()
    return s or None


def _agent_total_key(prefix: str, stamp: str, agent_id: str) -> str:
    """Per-agent window aggregate hash (model/task collapsed) — lets over_budget
    read one key (single HGETALL) instead of a KEYS glob scan. record_usage
    maintains it in lock-step with the fine-grained counters; same namespace +
    TTL window."""
    return f"{prefix}:{stamp}:__agent__:{_sanitize(agent_id)}"


async def _window_total(prefix: str, stamp: str, agent_id: str) -> int:
    """The agent's total tokens in the given window — a single HGETALL of the
    per-agent aggregate hash. Degrades to 0 when Redis is down or the bucket is
    missing (→ fail-open in over_budget)."""
    agg = await redis_client.hgetall(_agent_total_key(prefix, stamp, agent_id))
    if not agg:
        return 0
    try:
        return int(agg.get("total", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def over_budget(agent_id: str, model: str) -> dict[str, Any]:
    """Return whether `agent_id` has blown its token budget (opt-in, fail-open).

    Result: ``{"over": bool, "scope": str|None, "fallback_model": str|None}``.

    Decision:
      * No daily AND no hourly budget configured → ``over=False`` (the default;
        quota enforcement is OFF, behavior identical to pre-P3). This is the
        common path and short-circuits before touching Redis.
      * Budget configured but Redis unavailable / count below budget →
        ``over=False`` (fail-open: never block on a metering outage).
      * Budget configured and the live agent total for that window ≥ budget →
        ``over=True`` with the breached scope and the configured fallback_model
        (None if unset → caller leaves the model unchanged). Also stamps the
        breaker marker ``circuit:token:{agent_id}`` with a TTL to the window end.

    Never raises.
    """
    result: dict[str, Any] = {"over": False, "scope": None, "fallback_model": None}
    try:
        daily_budget = _read_budget("config:token:daily_budget")
        hourly_budget = _read_budget("config:token:hourly_budget")
        # Default path: no budget at all → enforcement off, no Redis touch.
        if daily_budget is None and hourly_budget is None:
            return result

        now = datetime.now(timezone.utc)
        # Hourly first (tighter window typically trips sooner), then daily.
        if hourly_budget is not None:
            used = await _window_total(_HOURLY_PREFIX, now.strftime("%Y%m%d%H"), agent_id)
            if used >= hourly_budget:
                return await _trip(result, "hourly", agent_id, _end_of_hour_ts(now))
        if daily_budget is not None:
            used = await _window_total(_DAILY_PREFIX, now.strftime("%Y%m%d"), agent_id)
            if used >= daily_budget:
                return await _trip(result, "daily", agent_id, _end_of_day_ts(now))
        return result
    except Exception as exc:  # noqa: BLE001 - fail-open on any error
        logger.debug("over_budget swallowed error (fail-open): %s", exc)
        return {"over": False, "scope": None, "fallback_model": None}


async def _trip(
    result: dict[str, Any], scope: str, agent_id: str, ttl_ts: int
) -> dict[str, Any]:
    """Mark the breaker open + populate the over-budget result. Best-effort
    marker write (a failed write only loses observability, not correctness)."""
    fb = _fallback_model()
    result["over"] = True
    result["scope"] = scope
    result["fallback_model"] = fb
    await redis_client.set_str(_breaker_key(agent_id), scope)
    await redis_client.expire_at(_breaker_key(agent_id), ttl_ts)
    logger.warning(
        "token quota tripped for agent=%s scope=%s → fallback_model=%s",
        agent_id, scope, fb,
    )
    return result


# ---------------------------------------------------------------------------
# Admin read: SQLite rollup aggregation (cost dashboard data source)
# ---------------------------------------------------------------------------

_WINDOW_DAYS = {"24h": 1, "7d": 7, "30d": 30, "90d": 90, "all": None}


def _window_days(window: str) -> Optional[int]:
    """Map a window label to a day count (None = all-time). Unknown → 7d."""
    return _WINDOW_DAYS.get(window, 7)


def usage_summary(db_path: str | Path, window: str = "7d") -> dict[str, Any]:
    """Aggregate the token_usage_rollup by model / task_type / agent + totals.

    Pure SQLite read (the flushed rollup). The admin endpoint overlays live
    Redis counts on top. ``window`` filters by the ``day`` column lexically
    (ISO YYYY-MM-DD sorts chronologically). Never raises on a missing table —
    ensure_schema runs first so an empty rollup yields zeroed aggregates."""
    ensure_schema(db_path)
    days = _window_days(window)
    where = ""
    params: tuple = ()
    if days is not None:
        cutoff = (datetime.now(timezone.utc) - _timedelta_days(days)).strftime("%Y-%m-%d")
        where = "WHERE day >= ?"
        params = (cutoff,)

    def _dim(col: str) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT {col} AS key, "
                "SUM(prompt_tokens) AS p, SUM(completion_tokens) AS c, "
                "SUM(total_tokens) AS t FROM token_usage_rollup "
                f"{where} GROUP BY {col} ORDER BY t DESC",
                params,
            ).fetchall()
        return [
            {
                "key": str(r["key"]),
                "prompt_tokens": int(r["p"] or 0),
                "completion_tokens": int(r["c"] or 0),
                "total_tokens": int(r["t"] or 0),
            }
            for r in rows
        ]

    by_model = _dim("model")
    by_task_type = _dim("task_type")
    by_agent = _dim("agent_id")
    totals = {
        "prompt_tokens": sum(r["prompt_tokens"] for r in by_model),
        "completion_tokens": sum(r["completion_tokens"] for r in by_model),
        "total_tokens": sum(r["total_tokens"] for r in by_model),
    }
    return {
        "window": window,
        "by_model": by_model,
        "by_task_type": by_task_type,
        "by_agent": by_agent,
        "totals": totals,
        "_agent_ids": [r["key"] for r in by_agent],
    }


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=days)


# ---------------------------------------------------------------------------
# Admin read: live Redis counters (overlay on the SQLite rollup)
# ---------------------------------------------------------------------------


async def live_today_totals(agent_ids: Optional[list[str]] = None) -> dict[str, int]:
    """Best-effort read of today's per-agent live totals from Redis. Returns
    ``{agent_id: total}``; empty when Redis is down. Used by the admin endpoint
    to overlay in-flight counts on top of the flushed SQLite rollup."""
    out: dict[str, int] = {}
    if not agent_ids:
        return out
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        for agent_id in agent_ids:
            total = await _window_total(_DAILY_PREFIX, stamp, agent_id)
            if total:
                out[agent_id] = total
    except Exception as exc:  # noqa: BLE001 - live overlay is best-effort
        logger.debug("live_today_totals swallowed error: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Test-only reset
# ---------------------------------------------------------------------------


def reset_for_tests() -> None:
    """Drop the process-level batcher singleton so a test re-inits cleanly.

    Test-only — production code never calls this. Does not await the batcher
    stop (tests that need a clean flush should call shutdown_batcher())."""
    global _BATCHER
    _BATCHER = None
    _task_type_var.set(DEFAULT_TASK_TYPE)
    _agent_id_var.set(DEFAULT_AGENT_ID)
