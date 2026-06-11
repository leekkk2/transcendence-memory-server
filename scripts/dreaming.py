#!/usr/bin/env python3
"""Dreaming engine — offline knowledge consolidation (blueprint P6, §A7).

The "dreaming" cycle is a periodic governance pass that, per enabled container:
  * **consolidates** high-frequency query patterns into a static Redis cache
    (a SAFE, additive action — it only writes new governance cache keys, never
    touches user data),
  * **proposes** (report-only) destructive cleanups (low-value vector fragments,
    LightRAG orphan nodes) as *candidates with counts*, and
  * records an index-card-candidate placeholder per container (the real index
    card compression tool is implemented by the governance toolbox, Agent B).

Every cycle emits a ``DreamReport`` written to the **isolated governance store**
(``governance_store`` — physically separate from user RAG corpora; see that
module's docstring for the RAG-immunity argument). This module adds NO filter to
the main query path: immunity is structural, not query-side.

Invariants (mirror redis_client.py P0 / config_store.py P1):

  * **Behavior-preserving deploy.** The background scheduler starts ONLY when
    ``config:dreaming:scheduler_enabled`` is true (default FALSE) — so deploying
    P6 spawns no background job and runtime is byte-identical. The manual
    ``/admin/dreaming/trigger`` endpoint is always available but never fires on
    its own. ``global_enabled`` (default true) is the "dreaming may run" gate;
    with it false a trigger short-circuits to ``skipped_global_disabled``.
  * **Report-only by default.** Destructive actions list candidates with
    ``applied=False`` unless ``dry_run=False`` AND ``config:dreaming:prune_apply``
    is true (default FALSE). P6 ships that real-delete branch guarded but
    NOT end-to-end verified — see followups.
  * **Graceful.** All Redis/config reads degrade (cfg_get returns default, etc.);
    nothing here raises into a caller. Optional scheduler deps (APScheduler /
    croniter) are lazily imported — missing → scheduler stays disabled, never
    breaks import or boot.
  * **No direct LLM.** P6 dreaming is report-only and does NOT call any model; if
    a future cycle needs an LLM it must reuse rag_engine's gateway (HR-9). No
    direct client / provider host appears here.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import redis_client  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import redis_client  # type: ignore

try:
    import config_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import config_store  # type: ignore

try:
    import governance_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import governance_store  # type: ignore

logger = logging.getLogger("transcendence-memory-server.dreaming")

# Redis namespace for consolidated high-frequency query caches. Distinct from
# the usage:tokens:* / circuit:* / cfg:* namespaces — additive governance keys.
_DREAM_CACHE_PREFIX = "dream:cache:hot"

# Daily query-frequency datasource (DR2). /search increments the count hash;
# the dream cycle reads it back and promotes hot queries. 48h TTL so yesterday's
# bucket is still readable by a nightly (post-midnight) cycle before evicting.
_QUERYFREQ_PREFIX = "usage:queryfreq:daily"
_QUERYFREQ_TEXT_PREFIX = "usage:queryfreq:text"  # hash → truncated query text
_QUERYFREQ_TTL_S = 48 * 3600
_HOT_CACHE_TTL_S = 24 * 3600
_QUERY_TEXT_MAXLEN = 120

# Container-level dreaming config key templates (§A7). Filled at runtime by
# container name — NOT registered statically in KNOWN_CONFIG (containers are
# dynamic). Read via redis_client.cfg_get / config_store fallback, all graceful.
def _ckey(container: str, leaf: str) -> str:
    return f"config:dreaming:container:{container}:{leaf}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Config resolution ───────────────────────────────────────────────────────


def _global_enabled() -> bool:
    """The global "dreaming may run" gate (default True). Pure config read."""
    return bool(config_store.get_cached("config:dreaming:global_enabled", True))


def _scheduler_enabled() -> bool:
    """Whether the background auto-scheduler should start (default FALSE).

    This is the behavior-preserving gate: false → deploying P6 spawns no job.
    """
    return bool(config_store.get_cached("config:dreaming:scheduler_enabled", False))


async def _cfg_container(container: str, leaf: str, default: Any) -> Any:
    """Read a container-level dreaming override, falling back to `default`.

    Tries Redis (cfg_get) first for the live cross-node value; on miss/Redis-down
    returns `default`. Container-level keys are dynamic so they live only in
    Redis/DB, not the KNOWN_CONFIG static cache. Never raises."""
    try:
        val = await redis_client.cfg_get(_ckey(container, leaf), None)
    except Exception:  # noqa: BLE001 - graceful: treat as unset
        val = None
    return default if val is None or val == "" else val


async def resolve_container_dream_config(container: str) -> dict[str, Any]:
    """Resolve effective {enabled, cron, model} for a container's dreaming.

    Container-level config (config:dreaming:container:{c}:*) overrides the global
    (config:dreaming:trigger_cron / batch_model). An unset container value
    inherits the global. When the global ``global_enabled`` gate is off, the
    container is reported disabled regardless of its own flag. Never raises —
    every read degrades to the global / static default.
    """
    if not _global_enabled():
        return {"enabled": False, "cron": None, "model": None}
    g_cron = str(config_store.get_cached("config:dreaming:trigger_cron", "0 2 * * *"))
    g_model = str(config_store.get_cached("config:dreaming:batch_model", ""))

    enabled_raw = await _cfg_container(container, "enabled", "true")
    enabled = str(enabled_raw).strip().lower() in ("1", "true", "yes", "on")
    cron = await _cfg_container(container, "cron", "")
    model = await _cfg_container(container, "model", "")
    return {
        "enabled": enabled,
        "cron": str(cron) if cron else g_cron,
        "model": str(model) if model else g_model,
    }


# ── Query-frequency datasource (DR2) ────────────────────────────────────────


def normalize_query(query: str) -> str:
    """strip + lower + collapse whitespace, so trivially-variant queries bucket
    together (the count is per query *pattern*, not per byte sequence)."""
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def query_hash(normalized: str) -> str:
    # 16 hex chars keep hash-field cardinality readable while collision odds
    # stay negligible at daily-query scale.
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def record_query_frequency(query: str) -> None:
    """Count one /search occurrence in today's frequency hash (fire-and-forget).

    Raw client ops (HINCRBY/HSET/EXPIRE) instead of cfg_* helpers because the
    datasource is hash-shaped; the graceful contract is identical — Redis
    down/disabled → silently skip, NEVER perturb the retrieval path."""
    norm = normalize_query(query)
    if not norm:
        return
    client = await redis_client.get_client()
    if client is None:
        return
    day = _utc_day()
    field = query_hash(norm)
    count_key = f"{_QUERYFREQ_PREFIX}:{day}"
    text_key = f"{_QUERYFREQ_TEXT_PREFIX}:{day}"
    try:
        await client.hincrby(count_key, field, 1)
        # hash → truncated original text, so the dream report stays readable.
        await client.hset(text_key, field, norm[:_QUERY_TEXT_MAXLEN])
        await client.expire(count_key, _QUERYFREQ_TTL_S)
        await client.expire(text_key, _QUERYFREQ_TTL_S)
    except Exception:  # noqa: BLE001 - counting must never raise into /search
        pass


# ── Dream cycle actions ─────────────────────────────────────────────────────


async def _hot_query_candidates(day: str) -> tuple[int, list[dict[str, Any]]]:
    """Return (scanned, candidates) from the day's frequency hash.

    A candidate is a query whose count reached config:dreaming:cache_threshold
    (live config read — Dashboard-tunable). Degrades to (0, []) on Redis down."""
    counts = await redis_client.hgetall(f"{_QUERYFREQ_PREFIX}:{day}")
    if not counts:
        return 0, []
    threshold = int(config_store.get_cached("config:dreaming:cache_threshold", 10))
    texts = await redis_client.hgetall(f"{_QUERYFREQ_TEXT_PREFIX}:{day}")
    candidates: list[dict[str, Any]] = []
    for field, raw in counts.items():
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        if count >= threshold:
            candidates.append(
                {"hash": field, "query": texts.get(field, ""), "count": count}
            )
    candidates.sort(key=lambda c: (-c["count"], c["hash"]))
    return len(counts), candidates


async def _promote_hot_queries(candidates: list[dict[str, Any]], day: str) -> int:
    """Write candidates to dream:cache:hot:<hash> (24h TTL). Returns writes done.

    Non-destructive additive cache write — within the report-only boundary (no
    user data is touched, the keys self-evict)."""
    written = 0
    for cand in candidates:
        payload = json.dumps(
            {"query": cand["query"], "count": cand["count"], "date": day},
            ensure_ascii=False,
        )
        key = f"{_DREAM_CACHE_PREFIX}:{cand['hash']}"
        if await redis_client.cfg_set(key, payload, ttl=_HOT_CACHE_TTL_S):
            written += 1
    return written


async def _consolidate_hot_queries(container: str, dry_run: bool = True) -> dict[str, Any]:
    """SAFE additive action: promote today's high-frequency queries (count ≥
    cache_threshold) into the static dream:cache:hot:* Redis cache. dry_run
    reports the candidates only; a real run also writes the cache keys. Never
    raises — Redis down degrades to a 'no_data' record."""
    day = _utc_day()
    scanned, candidates = await _hot_query_candidates(day)
    base: dict[str, Any] = {
        "tool": "consolidate_hot_queries",
        "container": container,
        "scanned": scanned,
        "candidates": len(candidates),
        "applied": False,
    }
    if scanned == 0:
        return {**base, "summary": "no_data"}
    if not candidates:
        return {**base, "summary": "no_candidates"}
    # Truncated-text digest + count per candidate, so the report is auditable.
    base["candidate_queries"] = [
        {"query": c["query"], "count": c["count"]} for c in candidates
    ]
    if dry_run:
        return {**base, "summary": "candidates_found"}
    written = await _promote_hot_queries(candidates, day)
    return {
        **base,
        "summary": "promoted_to_hot_cache",
        "written": written,
        "applied": written > 0,
    }


def _prune_candidates(container: str, apply_allowed: bool) -> list[dict[str, Any]]:
    """REPORT-ONLY destructive proposals: low-value vector fragments + LightRAG
    orphan nodes. P6 only LISTS candidates (count) with applied=False. The real
    delete is a guarded branch that runs ONLY when apply_allowed is True (dry_run
    False AND config:dreaming:prune_apply True) — NOT enabled / verified in P6.
    """
    graph_prune = bool(
        config_store.get_cached("config:dreaming:graph_prune_enabled", True)
    )
    actions: list[dict[str, Any]] = [
        {
            "tool": "prune_low_value_vectors",
            "container": container,
            "summary": "candidate_scan_report_only",
            "candidates": 0,  # no fragment-scan datasource wired in P6
            "applied": False,
        }
    ]
    if graph_prune:
        actions.append(
            {
                "tool": "prune_graph_orphans",
                "container": container,
                "summary": "candidate_scan_report_only",
                "candidates": 0,  # no graph-orphan scan wired in P6
                "applied": False,
            }
        )
    if apply_allowed:
        # GUARDED real-delete branch. P6 does NOT exercise this end-to-end; the
        # branch exists so the switch is honored once a real scan + delete is
        # implemented. Today there are zero candidates so nothing is deleted, but
        # we mark the intent for auditability.
        for a in actions:
            a["summary"] = "apply_requested_but_no_candidates"
            a["applied"] = a["candidates"] > 0
    return actions


def _index_card_placeholder(container: str) -> dict[str, Any]:
    """Index-card consolidation placeholder (§A3). The real LLM clustering /
    compression tool (compress_knowledge_cluster) is implemented by the
    governance toolbox (Agent B); the dream cycle only records the candidacy."""
    return {
        "tool": "index_card_candidate",
        "container": container,
        "summary": "deferred_to_governance_toolbox",
        "candidates": 0,
        "applied": False,
    }


async def _run_for_container(
    container: str, dry_run: bool, prune_apply_cfg: bool
) -> list[dict[str, Any]]:
    """Run all per-container dream actions, returning their action records."""
    apply_allowed = (not dry_run) and prune_apply_cfg
    actions: list[dict[str, Any]] = []
    actions.append(await _consolidate_hot_queries(container, dry_run))
    actions.append(_index_card_placeholder(container))
    actions.extend(_prune_candidates(container, apply_allowed))
    return actions


def _list_enabled_containers(scope: Optional[str]) -> list[str]:
    """Resolve the container scope for a cycle.

    A non-None `scope` runs just that container. None scans the known-container
    listing (reused from the server's filesystem container enumeration via a lazy
    import; degrades to [] if unavailable so a cycle on no containers is a no-op).
    """
    if scope:
        return [scope]
    try:  # lazy: avoid importing the heavy server module at import time
        try:
            import task_rag_server  # type: ignore
        except ModuleNotFoundError:
            from scripts import task_rag_server  # type: ignore
        dirs = task_rag_server._list_container_dirs()  # noqa: SLF001 - reuse listing
        return [p.name for p in dirs]
    except Exception:  # noqa: BLE001 - no listing → empty scope (no-op cycle)
        return []


async def run_dream_cycle(
    container: Optional[str] = None, dry_run: bool = True
) -> dict[str, Any]:
    """Run one dreaming cycle and return a DreamReport dict.

    With ``global_enabled`` false → returns ``{status:'skipped_global_disabled'}``
    immediately (the cycle is a no-op). Otherwise iterates the in-scope enabled
    containers, runs SAFE consolidation + REPORT-ONLY prune candidate scans, writes
    the report to the isolated governance store (stamped exclude_from_rag), and
    returns it. Never raises — a failure mid-cycle is captured into the report's
    notes and a degraded report is still returned.
    """
    started = _now_iso()
    scope_label = container or "all"
    if not _global_enabled():
        return {
            "status": "skipped_global_disabled",
            "started_at": started,
            "finished_at": _now_iso(),
            "container_scope": scope_label,
            "dry_run": dry_run,
            "excluded_from_rag": True,
            "actions": [],
            "notes": "config:dreaming:global_enabled is false; no dreaming performed.",
        }

    prune_apply_cfg = bool(config_store.get_cached("config:dreaming:prune_apply", False))
    actions: list[dict[str, Any]] = []
    notes_parts: list[str] = []
    try:
        targets = _list_enabled_containers(container)
        for name in targets:
            cfg = await resolve_container_dream_config(name)
            if not cfg.get("enabled"):
                continue
            actions.extend(await _run_for_container(name, dry_run, prune_apply_cfg))
        notes_parts.append(f"scanned {len(targets)} container(s)")
    except Exception as exc:  # noqa: BLE001 - cycle must not raise; degrade report
        logger.warning("[dreaming] cycle degraded: %s", exc)
        notes_parts.append(f"degraded: {exc}")

    if not dry_run and not prune_apply_cfg:
        notes_parts.append("prune_apply disabled — destructive actions report-only")
    report = {
        "status": "ok",
        "started_at": started,
        "finished_at": _now_iso(),
        "container_scope": scope_label,
        "dry_run": dry_run,
        "excluded_from_rag": True,
        "actions": actions,
        "notes": "; ".join(notes_parts) or "no enabled containers",
    }
    # Persist to the ISOLATED governance store (RAG-immune by structure).
    if not governance_store.write_dream_report(report):
        report["notes"] += " | report not persisted (governance store down)"
    return report


# ── Status ──────────────────────────────────────────────────────────────────


async def get_dream_status() -> dict[str, Any]:
    """Return the dreaming subsystem status for GET /admin/dreaming/status.

    Reflects the global gate, scheduler config + actual running state, cron /
    batch model, the last persisted report, and per-container resolved config for
    the known containers. Never raises — degrades to empty containers list.
    """
    containers: list[dict[str, Any]] = []
    try:
        for name in _list_enabled_containers(None):
            cfg = await resolve_container_dream_config(name)
            # raw per-container overrides (None when inheriting global)
            raw_cron = await _cfg_container(name, "cron", "")
            raw_model = await _cfg_container(name, "model", "")
            containers.append(
                {
                    "container": name,
                    "enabled": bool(cfg.get("enabled")),
                    "cron": str(raw_cron) or None,
                    "model": str(raw_model) or None,
                }
            )
    except Exception as exc:  # noqa: BLE001 - status must not raise
        logger.debug("[dreaming] status containers degraded: %s", exc)
    return {
        "global_enabled": _global_enabled(),
        "scheduler_enabled": _scheduler_enabled(),
        "scheduler_running": _scheduler_running(),
        "trigger_cron": str(config_store.get_cached("config:dreaming:trigger_cron", "0 2 * * *")),
        "batch_model": str(config_store.get_cached("config:dreaming:batch_model", "")),
        "last_report": governance_store.get_last_report(),
        "containers": containers,
    }


# ── Optional background scheduler ───────────────────────────────────────────
# Starts ONLY when config:dreaming:scheduler_enabled is true (default false) →
# deploying P6 spawns no background job. Uses APScheduler's AsyncIOScheduler when
# available; lazily imported so a missing package keeps the scheduler disabled
# and never breaks import or boot (mirror redis optional-dep range).

_SCHEDULER: Any = None  # AsyncIOScheduler instance | None


def _scheduler_running() -> bool:
    return _SCHEDULER is not None


async def start_scheduler() -> bool:
    """Start the background dreaming scheduler iff scheduler_enabled is true.

    Returns True if a scheduler was started, False otherwise (disabled by config,
    APScheduler missing, or already running). Safe in lifespan startup; never
    raises — any failure logs a warning and leaves the scheduler disabled, which
    preserves current behavior (no background job).
    """
    global _SCHEDULER
    if _SCHEDULER is not None:
        return True
    if not _scheduler_enabled():
        logger.info("[dreaming] scheduler disabled (config:dreaming:scheduler_enabled=false)")
        return False
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
        from apscheduler.triggers.cron import CronTrigger  # type: ignore
    except Exception as exc:  # noqa: BLE001 - missing dep → stay disabled, never raise
        logger.warning(
            "[dreaming] APScheduler unavailable (%s) — scheduler stays disabled", exc
        )
        return False
    try:
        cron = str(config_store.get_cached("config:dreaming:trigger_cron", "0 2 * * *"))
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            _scheduled_cycle,
            CronTrigger.from_crontab(cron, timezone="UTC"),
            id="dreaming_cycle",
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        _SCHEDULER = scheduler
        logger.info("[dreaming] background scheduler started (cron=%s)", cron)
        return True
    except Exception as exc:  # noqa: BLE001 - start failure must not break boot
        logger.warning("[dreaming] scheduler start failed, staying disabled: %s", exc)
        _SCHEDULER = None
        return False


async def _scheduled_cycle() -> None:
    """The job the scheduler fires: a full-scope dry-run dreaming cycle. Report-
    only by default (prune_apply gates any real delete). Swallows all errors."""
    try:
        await run_dream_cycle(container=None, dry_run=True)
    except Exception as exc:  # noqa: BLE001 - scheduled job must never crash the loop
        logger.warning("[dreaming] scheduled cycle error (swallowed): %s", exc)


async def stop_scheduler() -> None:
    """Shut down the scheduler if running. Safe in lifespan shutdown; never raises."""
    global _SCHEDULER
    scheduler = _SCHEDULER
    _SCHEDULER = None
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - shutdown must not raise
        pass


def reset_for_tests() -> None:
    """Drop the process-level scheduler singleton so a test re-inits cleanly.

    Test-only — production never calls this. Does not await a running scheduler's
    shutdown (tests that need that should call stop_scheduler())."""
    global _SCHEDULER
    _SCHEDULER = None
