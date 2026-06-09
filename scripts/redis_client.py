#!/usr/bin/env python3
"""Process-level lazy Redis client with graceful degradation.

This is the blueprint P0 foundation. Redis backs governance state — config
hot-reload, token counters, circuit breakers. Everything in this module is
built around one invariant:

    **Redis being unreachable MUST NEVER break the main RAG path.**

Design contract:
  * Import-safe — importing this module opens NO connection and never touches
    the network. A pool is created lazily on first `init_pool()` / `get_client()`.
  * `TM_REDIS_ENABLED=0` (or no REDIS_URL resolvable) → Redis is treated as
    permanently disabled; every accessor short-circuits to the degraded path.
  * `is_available()` does a short-timeout ping and swallows ALL exceptions,
    returning False rather than raising.
  * Safe read/write helpers (`cfg_get`, ...) return their `default` on any
    failure (Redis down, missing key, decode error) — they never raise.

`redis.asyncio` is an optional import: if the `redis` package is absent (e.g. a
slim test env) the module still imports and behaves as permanently-disabled.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("transcendence-memory-server.redis")

# Optional dependency — never let a missing/broken `redis` install break import.
try:
    import redis.asyncio as aioredis  # type: ignore
    from redis.exceptions import RedisError  # type: ignore

    _REDIS_IMPORTABLE = True
except Exception:  # pragma: no cover - environment without redis installed
    aioredis = None  # type: ignore
    RedisError = Exception  # type: ignore
    _REDIS_IMPORTABLE = False


# Short timeouts everywhere: a degraded/blocked Redis must fail fast so the
# caller falls back instead of stalling the request path.
_SOCKET_TIMEOUT = float(os.environ.get("TM_REDIS_SOCKET_TIMEOUT", "2.0"))
_SOCKET_CONNECT_TIMEOUT = float(os.environ.get("TM_REDIS_CONNECT_TIMEOUT", "2.0"))
_MAX_CONNECTIONS = int(os.environ.get("TM_REDIS_MAX_CONNECTIONS", "50"))
_HEALTH_CHECK_INTERVAL = int(os.environ.get("TM_REDIS_HEALTH_CHECK_INTERVAL", "30"))

# Process-level singletons. Created lazily; never at import time.
_POOL: Any = None
_CLIENT: Any = None
# Latched once we've logged the "disabled/degraded" reason, to avoid log spam.
_DISABLED_LOGGED = False


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def is_enabled() -> bool:
    """Whether Redis integration is enabled by configuration.

    Disabled when TM_REDIS_ENABLED is explicitly falsy, when the `redis`
    package is not importable, or when no connection URL can be resolved.
    This is a pure config check — it does NOT touch the network.
    """
    if not _REDIS_IMPORTABLE:
        return False
    enabled_raw = os.environ.get("TM_REDIS_ENABLED")
    # Default ON when unset, so a plain `docker compose up` wires Redis in.
    if enabled_raw is not None and not _truthy(enabled_raw):
        return False
    return _resolve_url() is not None


def _resolve_url() -> Optional[str]:
    """Resolve the Redis connection URL from env, or None if unconfigured.

    Priority: REDIS_URL > (REDIS_HOST/REDIS_PORT[/REDIS_PASSWORD]) composed URL.
    """
    url = (os.environ.get("REDIS_URL") or "").strip()
    if url:
        return url
    host = (os.environ.get("REDIS_HOST") or "").strip()
    if not host:
        return None
    port = (os.environ.get("REDIS_PORT") or "6379").strip()
    password = (os.environ.get("REDIS_PASSWORD") or "").strip()
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{host}:{port}/0"


def _log_disabled_once(reason: str) -> None:
    global _DISABLED_LOGGED
    if not _DISABLED_LOGGED:
        logger.warning("[redis] disabled/degraded: %s", reason)
        _DISABLED_LOGGED = True


async def init_pool() -> bool:
    """Idempotently build the connection pool + client (no eager connect).

    Returns True if a client was created (or already exists), False when Redis
    is disabled by config or the `redis` package is unavailable. Safe to call
    from a FastAPI lifespan startup; never raises.
    """
    global _POOL, _CLIENT
    if not is_enabled():
        _log_disabled_once(
            "TM_REDIS_ENABLED=0 / no REDIS_URL / redis package missing"
        )
        return False
    if _CLIENT is not None:
        return True
    url = _resolve_url()
    try:
        _POOL = aioredis.ConnectionPool.from_url(
            url,
            max_connections=_MAX_CONNECTIONS,
            socket_timeout=_SOCKET_TIMEOUT,
            socket_connect_timeout=_SOCKET_CONNECT_TIMEOUT,
            retry_on_timeout=True,
            health_check_interval=_HEALTH_CHECK_INTERVAL,
            # Governance keys/values are UTF-8 strings; decode so callers get
            # `str` not `bytes`. Helpers handle their own coercion.
            decode_responses=True,
        )
        # Bind the client to the pool we just built (URL already parsed above by
        # from_url) so the whole process shares one pool via connection_pool=.
        _CLIENT = aioredis.Redis(connection_pool=_POOL)
        # NOTE: building the pool/client does NOT open a socket — the first
        # command (or our startup ping) does. So this stays cheap + non-fatal.
        return True
    except Exception as exc:  # noqa: BLE001 - init must never crash the server
        logger.warning("[redis] init_pool failed, running degraded: %s", exc)
        _POOL = None
        _CLIENT = None
        return False


async def get_client() -> Any:
    """Return the shared async client, or None if Redis is unavailable.

    Lazily initialises the pool on first call. Never raises.
    """
    if _CLIENT is None:
        await init_pool()
    return _CLIENT


async def is_available() -> bool:
    """Best-effort liveness probe: True only if a ping round-trips.

    Swallows every exception (connection refused, timeout, auth, decode) and
    returns False. This is the function gates/fallback code should consult
    before assuming Redis is usable.
    """
    client = await get_client()
    if client is None:
        return False
    try:
        return bool(await client.ping())
    except Exception:  # noqa: BLE001 - probe must never raise
        return False


async def close_pool() -> None:
    """Idempotently tear down client + pool. Safe in lifespan shutdown."""
    global _POOL, _CLIENT
    client, pool = _CLIENT, _POOL
    _CLIENT = None
    _POOL = None
    if client is not None:
        try:
            await client.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
    if pool is not None:
        try:
            await pool.disconnect(inuse_connections=True)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass


# ── Safe read/write helpers ────────────────────────────────────────────────
# All return `default` (reads) / False (writes) on ANY failure. Callers can use
# them unconditionally without guarding for Redis being down.


async def cfg_get(key: str, default: Any = None) -> Any:
    """GET `key`, returning its value or `default`.

    Returns `default` when Redis is down/disabled, the key is missing, or any
    error occurs. Never raises.
    """
    client = await get_client()
    if client is None:
        return default
    try:
        value = await client.get(key)
        return default if value is None else value
    except Exception:  # noqa: BLE001 - degrade to default, never raise
        return default


async def cfg_set(key: str, value: Any, ttl: Optional[int] = None) -> bool:
    """SET `key`=`value` (optional TTL seconds). Returns success boolean.

    Returns False when Redis is down/disabled or any error occurs; never raises.
    """
    client = await get_client()
    if client is None:
        return False
    try:
        await client.set(key, value, ex=ttl)
        return True
    except Exception:  # noqa: BLE001 - degrade to no-op, never raise
        return False


def reset_for_tests() -> None:
    """Drop cached singletons so a test can re-init under fresh env.

    Test-only helper — production code never calls this. Does not await the
    async close (tests that need the socket closed should call close_pool()).
    """
    global _POOL, _CLIENT, _DISABLED_LOGGED
    _POOL = None
    _CLIENT = None
    _DISABLED_LOGGED = False
