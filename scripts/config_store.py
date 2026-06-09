"""Three-tier config center with Pub/Sub hot-reload (blueprint P1).

This is the runtime config plane that lets a future Dashboard tune scalar RAG
knobs **without a redeploy** — and have every node pick up the change live.

Three tiers, source-of-truth flows top→down:

    SQLite config_kv  ── persistent truth (survives restart) ──┐
            │                                                  │
            ▼  (set() writes here first; load_all reads it)    │
    Redis cfg_get/cfg_set  ── cross-node fast cache + the      │
            │                  `config_updated` Pub/Sub bus    │
            ▼                                                  │
    _CONFIG_CACHE (process dict)  ── what request paths read ◀─┘
            ▲   (sync get_cached, no await — hot path safe)

Invariants (the soul of this module — mirror redis_client.py P0):

  * **Graceful degradation is mandatory.** If Redis OR the config DB is
    unreachable, every read falls back to the caller-supplied `default`
    (which callers wire to the current profiles.yaml static value). Reads
    NEVER raise, NEVER block, NEVER touch the main RAG path's correctness.
  * **Behavior-preserving.** With no override present, `get_cached(key,
    default)` returns `default` verbatim — so `/search` / `/query` stay
    byte-identical to pre-P1 (similarity_threshold still None, citation still
    True) until someone explicitly sets an override.

This round only `similarity_threshold` and `citation_enabled` have a live reader
wired into `/search` & `/query`; `fallback_template`, `degradation_timeout_ms`,
and the `model:*` keys are registered + persistable placeholders with no live
reader yet (P2).
  * **Import-safe.** Importing opens no connection and touches no network.

Hot-reload data flow:

    node A: set(k,v) → DB write → Redis cfg_set → publish('config_updated',
            {changed_keys:[k]}) → local cache update
    node B: start_config_subscriber() background task receives the message →
            refresh([k]) re-reads DB/Redis → updates its _CONFIG_CACHE

HR-9 guard: `config:model:base_url:*` set is rejected unless the value points
at the sanctioned gateway host; `config:model:api_keys:*` is encrypted
write-only (Fernet under TM_CONFIG_SECRET) and NEVER echoed in logs/responses.

DEFER (P1b, intentionally NOT in this round): runtime atomic hot-swap of the
embedding_registry singleton (model/provider). That carries concurrency-tear
risk (in-flight queries reading a half-swapped registry) and is left to a
dedicated phase. P1 only hot-reloads memory-cache-safe scalar RAG config.

R8: pure generic code — no private endpoint / hostname / credential / private
container name. The only host literal is the public gateway host policy below.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

try:
    import redis_client  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import redis_client  # type: ignore

logger = logging.getLogger("transcendence-memory-server.config")

# The Pub/Sub channel every node listens on for live config changes.
CONFIG_CHANNEL = "config_updated"

# HR-9: model base_url overrides may only point at the sanctioned LLM gateway.
# The allowed host is injected by the deployment via TM_ALLOWED_MODEL_HOST (keeps
# the real private gateway subdomain out of this public repo per R8); the default
# is a placeholder so the host-pin guard still functions out of the box. No
# scheme/port — host-pinned only. A set with any other host is rejected.
_ALLOWED_MODEL_BASE_HOST = (
    os.environ.get("TM_ALLOWED_MODEL_HOST") or "newapi.example"
).strip().lower()


# ── Type coercion ───────────────────────────────────────────────────────────
# Stored values are TEXT in SQLite / strings in Redis. Each known key declares
# how to coerce its raw string back to a typed Python value. Coercers must be
# total + lossless for the round-trip and degrade to `default` on bad input via
# the caller (get_cached wraps them).


def _coerce_float_or_none(raw: Any) -> Optional[float]:
    """Empty / 'none' / 'null' → None (the opt-in OFF sentinel); else float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s == "" or s.lower() in ("none", "null"):
        return None
    return float(s)


def _coerce_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _coerce_int(raw: Any) -> int:
    if isinstance(raw, bool):  # guard: bool is an int subclass
        return int(raw)
    return int(str(raw).strip())


def _coerce_int_or_none(raw: Any) -> Optional[int]:
    """Empty / 'none' / 'null' → None (the unlimited / OFF sentinel); else int.

    Mirrors _coerce_float_or_none for the token budget keys: an absent or
    explicitly-cleared budget reads back as None so over_budget() treats it as
    "no limit configured" (quota enforcement off = pre-P3 behavior)."""
    if raw is None:
        return None
    if isinstance(raw, bool):  # guard: bool is an int subclass
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    if s == "" or s.lower() in ("none", "null"):
        return None
    return int(s)


def _coerce_str(raw: Any) -> str:
    return "" if raw is None else str(raw)


# ── Known config registry ───────────────────────────────────────────────────
# Only keys registered here are accepted by set(). Each entry: the coercer to
# apply on read, and whether the value is sensitive (encrypted write-only).
# This round only similarity_threshold + citation_enabled have a live reader in
# the RAG path (truly hot-reloaded); the rest — fallback_template,
# degradation_timeout_ms, and the model:* keys — are registered as placeholders
# so a future Dashboard / P2 can persist them without a schema change (they have
# no live reader yet).


class _ConfigKey:
    __slots__ = ("coerce", "sensitive", "typename", "default")

    def __init__(
        self,
        coerce: Callable[[Any], Any],
        sensitive: bool = False,
        typename: str = "str",
        default: Any = None,
    ):
        self.coerce = coerce
        self.sensitive = sensitive
        # typename / default are PURE METADATA for the read-only Dashboard config
        # endpoint (P2 GET /admin/config) — they have NO effect on get_cached /
        # set / refresh runtime behavior (the live RAG-path readers pass their own
        # profiles.yaml-derived default to get_cached, which still wins). `default`
        # here is the registered "no-override" sentinel the Dashboard shows when a
        # key has never been set; for keys whose true default is the live
        # profiles.yaml value (similarity_threshold / citation_enabled) it is the
        # opt-in sentinel (None / True) the request path falls back to pre-P1.
        self.typename = typename
        self.default = default


KNOWN_CONFIG: dict[str, _ConfigKey] = {
    # ── Live RAG knobs — actually hot-reloaded into /search & /query this round ─
    "config:rag:similarity_threshold": _ConfigKey(
        _coerce_float_or_none, typename="float", default=None
    ),
    "config:rag:citation_enabled": _ConfigKey(
        _coerce_bool, typename="bool", default=True
    ),
    # ── Registered + persistable, but no live reader yet (P2) ───────────────
    "config:rag:fallback_template": _ConfigKey(
        _coerce_str, typename="str", default=""
    ),
    "config:rag:degradation_timeout_ms": _ConfigKey(
        _coerce_int, typename="int", default=0
    ),
    "config:model:base_url:llm": _ConfigKey(
        _coerce_str, typename="str", default=""
    ),
    "config:model:base_url:embedding": _ConfigKey(
        _coerce_str, typename="str", default=""
    ),
    "config:model:api_keys:llm": _ConfigKey(
        _coerce_str, sensitive=True, typename="str", default=""
    ),
    "config:model:api_keys:embedding": _ConfigKey(
        _coerce_str, sensitive=True, typename="str", default=""
    ),
    # ── Token metering / quota breaker (blueprint P3, §A6) ──────────────────
    # Opt-in: the live reader (token_meter.over_budget) passes default=None for
    # the budgets, so with no override present quota enforcement is OFF (= pre-P3
    # behavior, byte-identical). daily/hourly_budget coerce to int|None: empty /
    # 'none' → None (the unlimited sentinel). fallback_model + flush_interval are
    # read with their own caller defaults.
    "config:token:daily_budget": _ConfigKey(
        _coerce_int_or_none, typename="int", default=None
    ),
    "config:token:hourly_budget": _ConfigKey(
        _coerce_int_or_none, typename="int", default=None
    ),
    "config:token:fallback_model": _ConfigKey(
        _coerce_str, typename="str", default=""
    ),
    "config:token:flush_interval": _ConfigKey(
        _coerce_int, typename="int", default=0
    ),
}

# Prefixes used by HR-9 guards (so adding more base_url:* / api_keys:* keys
# above keeps the guard coverage automatic).
_BASE_URL_PREFIX = "config:model:base_url:"
_API_KEYS_PREFIX = "config:model:api_keys:"


# ── Process-level runtime state ─────────────────────────────────────────────
# _CONFIG_CACHE holds RAW (string/None) values exactly as persisted; get_cached
# coerces on read. _CACHE_LOCK guards the dict for the rare concurrent
# subscriber-refresh vs request-read.
_CONFIG_CACHE: dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_SUBSCRIBER_TASK: Any = None  # asyncio.Task | None, set by start_config_subscriber
# Latched warning so a missing encryption secret logs once, not per write.
_SECRET_WARNED = False


# ── SQLite façade ───────────────────────────────────────────────────────────
# Short-lived per-call connections (sqlite3 threading model) — same pattern as
# JobQueue / BacklogStore. The config_kv table lives in the SAME queue.db file
# (single WAL / crash / purge domain) per the P1 spec.


class ConfigKVStore:
    """Persistent truth for config overrides: a `config_kv` table."""

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
                CREATE TABLE IF NOT EXISTS config_kv (
                    key        TEXT PRIMARY KEY,
                    value      TEXT,
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                """
            )

    def get(self, key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM config_kv WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else row["value"]

    def get_row(self, key: str) -> tuple[bool, Optional[str]]:
        """Like get(), but distinguishes a present row (possibly NULL value) from
        an absent row. Returns (found, value). `found=True, value=None` means the
        override was explicitly cleared (set to NULL); `found=False` means no row.
        Lets refresh() propagate an override-clear vs. fall back on a real miss.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM config_kv WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return (False, None)
        return (True, row["value"])

    def all(self) -> dict[str, Optional[str]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM config_kv").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set(self, key: str, value: Optional[str], updated_at: int) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO config_kv (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at),
            )


# Lazy DB singleton — resolved from WORKSPACE so it shares queue.db with the
# job queue. Created on first use; never at import time.
_STORE: Optional[ConfigKVStore] = None


def _queue_db_path() -> Path:
    """Same resolution as task_rag_server._queue_db_path (kept local to avoid a
    circular import). WORKSPACE drives the path so tests can isolate it."""
    import os

    ws = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
    return ws / "tasks" / "rag" / "queue.db"


def _get_store() -> Optional[ConfigKVStore]:
    """Lazily build the ConfigKVStore; return None if the DB can't be opened.

    A None here is the degraded path: the config plane runs cache-only (which,
    when empty, means every get_cached returns its default → behavior-preserving).
    """
    global _STORE
    if _STORE is not None:
        return _STORE
    try:
        _STORE = ConfigKVStore(_queue_db_path())
        return _STORE
    except Exception as exc:  # noqa: BLE001 - never let DB init break callers
        logger.warning("[config] DB init failed, running cache-only: %s", exc)
        return None


# ── Encryption helper (sensitive api_keys) ──────────────────────────────────
# TM_CONFIG_SECRET present → Fernet encrypt at rest; absent → plaintext at rest
# WITH a one-time warning (acceptable under the private-repo threat model), but
# NEVER echoed to logs/responses either way.


def _fernet():
    """Return a Fernet instance from TM_CONFIG_SECRET, or None if unavailable.

    None means "no encryption" — caller stores plaintext + warns once. Missing
    cryptography package or a malformed key both degrade to None (never raise).
    """
    import os

    secret = (os.environ.get("TM_CONFIG_SECRET") or "").strip()
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet  # type: ignore

        return Fernet(secret.encode("utf-8"))
    except Exception:  # noqa: BLE001 - bad/absent crypto degrades to plaintext
        return None


_ENC_PREFIX = "enc:"  # marks a value stored as Fernet ciphertext


def _encrypt_sensitive(value: str) -> str:
    """Encrypt a sensitive value for storage. Falls back to plaintext + warn."""
    global _SECRET_WARNED
    f = _fernet()
    if f is None:
        if not _SECRET_WARNED:
            logger.warning(
                "[config] TM_CONFIG_SECRET unset — sensitive config stored in "
                "plaintext (acceptable under private-repo threat model). Value "
                "is never logged or echoed."
            )
            _SECRET_WARNED = True
        return value
    try:
        return _ENC_PREFIX + f.encrypt(value.encode("utf-8")).decode("utf-8")
    except Exception:  # noqa: BLE001 - encryption failure degrades to plaintext
        return value


# ── Sync read (request hot path) ────────────────────────────────────────────


def get_cached(key: str, default: Any = None) -> Any:
    """Read a config value from the process cache, coerced; else `default`.

    SYNC + non-blocking — safe to call from request handlers without await. The
    cache is populated by load_all() at startup and refresh() on Pub/Sub events.
    When the key is absent (no override) OR coercion fails, returns `default`
    verbatim — this is what preserves pre-P1 behavior. Sensitive keys are never
    decrypted here (they have no request-path reader); callers must not pass
    api_keys:* to get_cached.
    """
    with _CACHE_LOCK:
        if key not in _CONFIG_CACHE:
            return default
        raw = _CONFIG_CACHE[key]
    spec = KNOWN_CONFIG.get(key)
    if spec is None:
        return default
    try:
        return spec.coerce(raw)
    except Exception:  # noqa: BLE001 - bad stored value must not break reads
        logger.warning("[config] coerce failed for %s; using default", key)
        return default


def _cache_put(key: str, raw: Any) -> None:
    with _CACHE_LOCK:
        _CONFIG_CACHE[key] = raw


def _cache_evict(key: str) -> None:
    with _CACHE_LOCK:
        _CONFIG_CACHE.pop(key, None)


def _cache_clear() -> None:
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()


# ── Async load / set / refresh ──────────────────────────────────────────────


async def load_all() -> None:
    """Populate _CONFIG_CACHE from the DB (the persistent truth) at startup.

    Redis, when available, is opportunistically refreshed from the DB so peers
    that come up later read a warm cache; but the DB is authoritative here. Any
    failure leaves the cache as-is (empty on cold start → all reads degrade to
    default), never raises. Sensitive values stay raw in cache (not decrypted).
    """
    store = _get_store()
    if store is None:
        logger.info("[config] no DB — config plane cache-only (defaults apply)")
        return
    try:
        rows = store.all()
    except Exception as exc:  # noqa: BLE001 - degrade to empty cache
        logger.warning("[config] load_all DB read failed, degraded: %s", exc)
        return
    loaded = 0
    for key, raw in rows.items():
        spec = KNOWN_CONFIG.get(key)
        if spec is None:
            continue  # ignore unknown rows (forward-compat with future keys)
        if spec.sensitive:
            continue  # write-only: never cache sensitive values (mirror set/refresh)
        _cache_put(key, raw)
        loaded += 1
        # Warm Redis so a freshly-joined peer reads a populated cfg cache. Best
        # effort: cfg_set degrades to no-op when Redis is down.
        await redis_client.cfg_set(key, "" if raw is None else raw)
    logger.info("[config] loaded %d override(s) from DB", loaded)


async def set(key: str, value: Any) -> bool:
    """Persist a config override and broadcast it. Returns success.

    Order: validate (known key + HR-9 guard) → coerce/serialize → DB write
    (the only layer whose failure means real failure) → Redis cfg_set (best
    effort) → publish config_updated (best effort) → local cache update.

    Redis being down only loses the live fan-out; the DB + local cache are
    already updated, so this node is correct and peers catch up on next
    load_all/restart. A DB failure returns False (nothing was persisted).
    Never raises — validation failures return False with a warning.
    """
    spec = KNOWN_CONFIG.get(key)
    if spec is None:
        logger.warning("[config] rejected set of unknown key %s", key)
        return False

    # HR-9: base_url overrides are host-pinned to the sanctioned gateway.
    if key.startswith(_BASE_URL_PREFIX) and not _base_url_allowed(value):
        logger.warning(
            "[config] rejected base_url override for %s — host must be %s",
            key,
            _ALLOWED_MODEL_BASE_HOST,
        )
        return False

    # Serialize to the stored string form. Sensitive → encrypt write-only.
    if value is None:
        stored: Optional[str] = None
    elif spec.sensitive:
        # An empty (or whitespace-only) value clears the secret: store a NULL row
        # (NOT an encrypted empty string) so describe_key reports configured=False
        # and the present-but-NULL row still propagates the clear to peers via
        # refresh() evict. This is the write-only "remove secret" path the
        # Dashboard uses (PUT value:'').
        stored = None if str(value).strip() == "" else _encrypt_sensitive(str(value))
    else:
        # Coerce first so we reject malformed values up front, then store the
        # canonical string. None coercion (float|None key) stores empty string.
        try:
            coerced = spec.coerce(value)
        except Exception:  # noqa: BLE001 - reject bad value, don't persist
            logger.warning("[config] rejected set of %s — bad value", key)
            return False
        stored = "" if coerced is None else str(coerced)

    store = _get_store()
    if store is None:
        logger.warning("[config] set of %s failed — no DB", key)
        return False
    try:
        import time

        store.set(key, stored, int(time.time()))
    except Exception as exc:  # noqa: BLE001 - DB write failure is real failure
        logger.warning("[config] set of %s failed at DB: %s", key, exc)
        return False

    # Best-effort propagation. Failures here are non-fatal (DB already truth).
    await redis_client.cfg_set(key, "" if stored is None else stored)
    await redis_client.publish(
        CONFIG_CHANNEL, json.dumps({"changed_keys": [key]})
    )
    # Update this process's cache immediately so the writer node is consistent
    # with what a peer converges to on refresh. A None `stored` means the
    # override was cleared: the DB keeps its present-but-NULL row (so peers'
    # refresh() can see it and _cache_evict), but THIS node must also evict —
    # NOT _cache_put(key, None) — otherwise get_cached finds a present NULL and
    # returns its coerced form (citation_enabled → False) instead of the caller's
    # static default (True). Evicting makes the writer fall back to the default,
    # matching the peer-refresh path exactly (set-to-default = reset regression).
    if not spec.sensitive:
        if stored is None:
            _cache_evict(key)
        else:
            _cache_put(key, stored)
    return True


async def refresh(keys: list[str]) -> None:
    """Re-read the given keys from DB (then Redis) into _CONFIG_CACHE.

    Called by the Pub/Sub subscriber when a peer broadcasts a change. The DB is
    authoritative: a present row (even with a NULL value = an explicitly cleared
    override) wins and is written straight into the cache — so clearing an
    override on one node converges every peer back to its default rather than
    serving a stale value. Only a real DB miss (no row) OR a DB read error falls
    back to Redis, then to leaving the cache untouched. Never raises. Sensitive
    keys are skipped (no request-path reader; read lazily by their consumer).
    """
    store = _get_store()
    for key in keys:
        spec = KNOWN_CONFIG.get(key)
        if spec is None or spec.sensitive:
            continue
        if store is not None:
            try:
                found, raw = store.get_row(key)
            except Exception:  # noqa: BLE001 - DB read error → try Redis
                found = False
                raw = None
            if found:
                if raw is None:
                    # Row present but NULL = override explicitly cleared.
                    # Evict so get_cached falls back to the caller default →
                    # every peer converges to default (no stale value served).
                    _cache_evict(key)
                else:
                    _cache_put(key, raw)
                continue
        # No DB / DB error / no row → try Redis as a secondary source; if Redis
        # also has nothing, leave the existing cache untouched (still valid).
        redis_val = await redis_client.cfg_get(key, None)
        if redis_val is not None:
            _cache_put(key, redis_val)


# ── Pub/Sub subscriber (background task) ────────────────────────────────────


async def start_config_subscriber() -> Any:
    """Spawn the background task that listens for `config_updated` and refreshes.

    Returns the asyncio.Task (or None if it couldn't start). The task loop:
    subscribe → for each message, parse changed_keys → refresh them. If Redis is
    down at start, make_pubsub returns None and we exit cleanly (no task). Any
    runtime error inside the loop is swallowed and the task ends — the node
    keeps its current cache (still valid) and a restart re-subscribes.
    """
    global _SUBSCRIBER_TASK
    import asyncio

    pubsub = await redis_client.make_pubsub(CONFIG_CHANNEL)
    if pubsub is None:
        logger.info("[config] Redis down — no live config subscriber (DB/cache only)")
        return None

    async def _loop() -> None:
        try:
            async for message in pubsub.listen():
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "message":
                    continue  # skip subscribe/unsubscribe confirmations
                try:
                    payload = json.loads(message.get("data") or "{}")
                    changed = payload.get("changed_keys") or []
                except Exception:  # noqa: BLE001 - ignore malformed broadcasts
                    continue
                if changed:
                    await refresh(list(changed))
        except asyncio.CancelledError:  # graceful shutdown
            raise
        except Exception as exc:  # noqa: BLE001 - loop must not crash the app
            logger.warning("[config] subscriber loop ended (degraded): %s", exc)
        finally:
            try:
                await pubsub.aclose()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - best-effort close
                pass

    _SUBSCRIBER_TASK = asyncio.create_task(_loop())
    logger.info("[config] live config subscriber started on %s", CONFIG_CHANNEL)
    return _SUBSCRIBER_TASK


async def stop_config_subscriber() -> None:
    """Cancel the subscriber task if running. Safe in lifespan shutdown.

    Awaiting a cancelled task re-raises asyncio.CancelledError (which is a
    BaseException, NOT caught by `except Exception`) — so it is caught explicitly
    here and swallowed; this is the intended clean shutdown, not an error. Any
    other exception the task ended with is also swallowed (shutdown never raises).
    """
    import asyncio

    global _SUBSCRIBER_TASK
    task = _SUBSCRIBER_TASK
    _SUBSCRIBER_TASK = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:  # expected clean cancellation — swallow
        pass
    except Exception:  # noqa: BLE001 - shutdown must not raise on anything else
        pass


# ── HR-9 helper ─────────────────────────────────────────────────────────────


def _base_url_allowed(value: Any) -> bool:
    """True only if `value` is a URL whose host is the sanctioned gateway.

    Liberal on scheme/port/path — pins only the host. An empty/None value is
    rejected (a base_url override must name the allowed host explicitly).
    """
    s = (str(value) if value is not None else "").strip()
    if not s:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(s).hostname or "").lower()
    except Exception:  # noqa: BLE001 - unparseable → reject
        return False
    return host == _ALLOWED_MODEL_BASE_HOST


# ── Read-only introspection (P2 Dashboard GET /admin/config) ────────────────
# Pure read helpers: enumerate KNOWN_CONFIG with each key's module / type /
# effective value / override flag / default, applying the SAME sensitive-masking
# discipline as /admin/profiles (never echo api_keys:* values — only a
# `configured: bool`). These never write, never raise, and degrade to defaults
# when the DB is unreachable (same graceful-degradation invariant as the rest of
# this module).


def module_for_key(key: str) -> str:
    """Derive the UI grouping module from a `config:<module>:<...>` key.

    The module is the segment right after the `config:` prefix (rag / model /
    token / ingest / dreaming / tools …). Falls back to the raw key when the
    shape is unexpected so the endpoint never crashes on a malformed registry.
    """
    parts = key.split(":")
    if len(parts) >= 2 and parts[0] == "config":
        return parts[1]
    return parts[0] if parts else key


def _effective_raw(key: str) -> tuple[bool, Optional[str]]:
    """Return (found, raw_db_value) for a key, reading the persistent DB.

    `found=True` iff a row exists in config_kv for this key (it has been `set()`
    at least once and not absent). Reuses ConfigKVStore.get_row (P1) so a
    present-but-NULL row (explicitly cleared override) reads as found=True,
    value=None — the caller (describe_key) then decides is_override by whether
    raw is a non-empty real value, so a cleared override is NOT shown as
    modified. Degrades to (False, None) when the DB is unreachable — same as a
    cold cache.
    """
    store = _get_store()
    if store is None:
        return (False, None)
    try:
        return store.get_row(key)
    except Exception:  # noqa: BLE001 - never let a read break the endpoint
        return (False, None)


def describe_key(key: str) -> dict[str, Any]:
    """Describe one known config key for the Dashboard config endpoint.

    Returns a dict with: key, module, type, is_override, default, and either
    `value` (non-sensitive: the current EFFECTIVE typed value — override if set,
    else the registered default) or, for sensitive keys, `value=None` +
    `configured: bool` (whether a non-empty override has been persisted). The
    sensitive value itself is NEVER read back / decrypted / echoed — identical
    discipline to /admin/profiles' `api_key_configured`.
    """
    spec = KNOWN_CONFIG[key]
    found, raw = _effective_raw(key)
    # is_override means the effective value TRULY differs from the registered
    # default — not merely "a row exists". A cleared override leaves a
    # present-but-NULL/'' row (kept so peers' refresh() can evict); that row must
    # NOT light up the UI 'modified' badge. So a present row whose value is
    # None/'' reads as is_override=False (= back to default). Only a present row
    # with a non-empty real value is a live override.
    is_override = found and raw not in (None, "")
    out: dict[str, Any] = {
        "key": key,
        "module": module_for_key(key),
        "type": spec.typename,
        "is_override": is_override,
        "default": spec.default,
    }
    if spec.sensitive:
        # Write-only: never surface the value. `configured` = a non-empty
        # override row exists (empty string / NULL = cleared = not configured) —
        # now identical to is_override for sensitive keys.
        out["value"] = None
        out["configured"] = is_override
        return out
    # Non-sensitive: effective value = override coerced to its type, else the
    # registered default. get_cached already coerces from the process cache, but
    # we coerce the DB raw directly here so the endpoint reflects persistent
    # truth even before a cache warm/refresh has run.
    if is_override:
        try:
            out["value"] = spec.coerce(raw)
        except Exception:  # noqa: BLE001 - bad stored value → show default
            out["value"] = spec.default
    else:
        out["value"] = spec.default
    return out


def describe_all() -> list[dict[str, Any]]:
    """Describe every known config key (stable KNOWN_CONFIG insertion order)."""
    return [describe_key(key) for key in KNOWN_CONFIG]


# ── Test-only reset ─────────────────────────────────────────────────────────


def reset_for_tests() -> None:
    """Drop process-level state so a test can re-init under fresh env/WORKSPACE.

    Test-only — production never calls this. Does not await subscriber cancel.
    """
    global _STORE, _SUBSCRIBER_TASK, _SECRET_WARNED
    _STORE = None
    _SUBSCRIBER_TASK = None
    _SECRET_WARNED = False
    _cache_clear()
