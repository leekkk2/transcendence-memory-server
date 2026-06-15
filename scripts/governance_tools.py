#!/usr/bin/env python3
"""Governance toolbox — tool registry + container×tool activation (blueprint P6, §A8).

This module is the **safe, opt-in governance action layer** that sits beside the
dreaming engine. It exposes six preset governance tools (§9.A), resolves whether
each is enabled for a given container (container-level override layered over the
global enable map), and invokes them. SAFE tools always run for real; the
LLM / destructive tools stay preview-only (plan) under the endpoint default
``dry_run=True`` and execute for real only on an explicit ``dry_run=false``:

  * snapshot_and_quarantine — reversible: full snapshot + move candidates to a
    sidecar quarantine file, never a hard delete;
  * compress_knowledge_cluster — additive: appends one LLM index card, sources
    are never deleted;
  * tune_model_parameters — guard-railed: allow-list + range-checked config
    writes through config_store (revert by clearing the override).

Why this is behavior-preserving on a fresh deploy:

  * **Read-only resolution + dry_run-by-default actions.** ``resolve_*`` are
    pure config reads. With the endpoint default ``dry_run=True``,
    ``invoke_tool`` only mutates state for the three SAFE tools
    (manage_token_quotas = read-only; analyze_retrieval_latency = read-only;
    update_container_routing = an additive config write through the SAME
    ``config_store.set`` the PUT /admin/config endpoint uses — no write bypass).
    The LLM / destructive tools touch data only on explicit dry_run=false.
  * **No RAG-path readers.** Nothing here is consulted by ``/search`` / ``/query``;
    registering / invoking a tool cannot change retrieval output.
  * **Structural RAG immunity.** Any governance artifact this layer would persist
    goes to the isolated ``governance_store`` (physically separate from user
    corpora) — there is no path from a tool write into a user container's LanceDB.

Invariants (mirror redis_client.py P0 / config_store.py P1 / dreaming.py P6):

  * **Import-safe** — importing opens no connection, touches no network.
  * **Graceful** — every Redis/config/DB read degrades to the conservative
    default (global map → preset defaults); nothing here raises into a caller.
  * **No direct LLM** (HR-9) — every LLM call here goes through
    ``_llm_oneshot`` → ``rag_engine._llm_func``, the env-driven (`LLM_*`)
    sanctioned-gateway entry; no provider endpoint is ever dialed directly.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
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

try:
    import token_meter  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import token_meter  # type: ignore

logger = logging.getLogger("transcendence-memory-server.governance_tools")


# ── Tool registry (blueprint §9.A) ──────────────────────────────────────────
# Each tool's static descriptor. `destructive` / `needs_llm` mark the tools
# that stay plan-only under the endpoint default dry_run=True and execute for
# real only on explicit dry_run=false (safe tools always run). `scope='global'`
# tools ignore the container dimension (manage_token_quotas).


class _Tool:
    __slots__ = ("name", "scope", "description", "destructive", "needs_llm")

    def __init__(
        self,
        name: str,
        scope: str,
        description: str,
        destructive: bool = False,
        needs_llm: bool = False,
    ) -> None:
        self.name = name
        self.scope = scope
        self.description = description
        self.destructive = destructive
        self.needs_llm = needs_llm

    @property
    def safe(self) -> bool:
        """A tool invoke_tool may run for real: neither destructive nor LLM-backed."""
        return not self.destructive and not self.needs_llm


TOOLS: dict[str, _Tool] = {
    "compress_knowledge_cluster": _Tool(
        "compress_knowledge_cluster", "container",
        "聚类压缩同主题记忆为高密度索引卡（需 LLM，经 rag_engine 网关；"
        "dry_run=false 真执行，附加式不删源）。",
        needs_llm=True,
    ),
    "update_container_routing": _Tool(
        "update_container_routing", "container",
        "更新容器路由规则（写 config:container:routing_rules，经 config_store 加性合并）。",
    ),
    "snapshot_and_quarantine": _Tool(
        "snapshot_and_quarantine", "container",
        "快照并隔离低价值/异常记忆（破坏性可逆：默认 dry_run 仅产计划，"
        "dry_run=false 真执行——全量快照+隔离文件留存，零硬删）。",
        destructive=True,
    ),
    "tune_model_parameters": _Tool(
        "tune_model_parameters", "container",
        "依检索质量调参（需 LLM 评估；dry_run=false 真执行，"
        "allow-list+范围护栏，经 config_store 加性写）。",
        needs_llm=True,
    ),
    "analyze_retrieval_latency": _Tool(
        "analyze_retrieval_latency", "container",
        "分析检索时延（只读现有计时指标）。",
    ),
    "manage_token_quotas": _Tool(
        "manage_token_quotas", "global",
        "查询/管理 token 配额与用量（全局作用域，只读余额/用量）。",
    ),
}


def tool_is_destructive(name: str) -> bool:
    """该工具是否破坏性（``TOOLS[name].destructive``）；未知工具名 → False
    （保守：未知工具不被当成破坏性，由上层 enable-map / dispatch 兜底）。
    供 agent 安全闸/审批层判定是否必须走人工审批。永不抛。"""
    spec = TOOLS.get(name)
    return bool(spec.destructive) if spec is not None else False


# Container-level tool override key template (§A8). Dynamic per container name —
# NOT registered in KNOWN_CONFIG; read/written via Redis + the config_store DB
# store directly (mirror dreaming's per-container key handling).
def _container_map_key(container: str) -> str:
    return f"config:tools:container:{container}:enabled_map"


def _global_enabled_map() -> dict[str, bool]:
    """The global per-tool master switch map (default: all preset tools on).

    Reads the registered ``config:tools:global_enabled_map`` json key via the
    coercing get_cached, falling back to the preset-all-true default. Always
    returns a dict[str, bool]; never raises."""
    default = {n: True for n in TOOLS}
    raw = config_store.get_cached("config:tools:global_enabled_map", default)
    if not isinstance(raw, dict):
        return dict(default)
    return {str(k): bool(v) for k, v in raw.items()}


def _new_tool_default() -> bool:
    """config:tools:new_tool_default_enabled (default False) — the fallback for a
    tool name absent from the global map (e.g. a future tool). Never raises."""
    return bool(config_store.get_cached("config:tools:new_tool_default_enabled", False))


async def read_container_raw_map(container: str) -> Optional[dict[str, bool]]:
    """Read a container's own enabled_map override, or None when it has none.

    The dynamic per-container key is not in KNOWN_CONFIG, so it lives only in
    Redis (live cross-node) + the config_store DB (persistent truth). Reads Redis
    first, then the DB; an unset / empty / malformed value reads back as None
    (full inheritance of the global map). Never raises — degrades to None.
    """
    raw: Any = None
    try:
        raw = await redis_client.cfg_get(_container_map_key(container), None)
    except Exception:  # noqa: BLE001 - Redis down → try the DB next
        raw = None
    if raw is None or raw == "":
        raw = _db_raw(_container_map_key(container))
    return _parse_bool_map(raw)


def _db_raw(key: str) -> Any:
    """Read a raw config value straight from the config_store persistent DB.

    Used for dynamic (non-KNOWN_CONFIG) container keys that get_cached won't
    serve. Returns None on any failure / absence — never raises."""
    try:
        store = config_store._get_store()  # noqa: SLF001 - intentional reuse of the DB façade
        return None if store is None else store.get(key)
    except Exception:  # noqa: BLE001 - DB down → treat as unset
        return None


def _parse_bool_map(raw: Any) -> Optional[dict[str, bool]]:
    """Coerce a stored enabled_map blob (json string / dict) to dict[str, bool].

    Empty / None / malformed → None (means "no override, inherit global"). Never
    raises."""
    if raw is None or raw == "":
        return None
    obj: Any = raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001 - malformed → no override
            return None
    if not isinstance(obj, dict) or not obj:
        return None
    return {str(k): bool(v) for k, v in obj.items()}


# ── Activation resolution ────────────────────────────────────────────────────


async def resolve_tool_enabled(tool: str, container: Optional[str] = None) -> bool:
    """Whether `tool` is enabled for `container` (container override > global).

    Resolution order (each step degrades gracefully):
      1. For a container-scoped tool with a per-container enabled_map override
         that names this tool → that wins.
      2. Else the global ``config:tools:global_enabled_map`` value for the tool.
      3. Else (tool absent from the global map, e.g. a brand-new tool) →
         ``config:tools:new_tool_default_enabled`` (default False).

    Global-scope tools (manage_token_quotas) ignore the container dimension. A
    Redis / config outage falls back to the global map / preset default — never
    raises.
    """
    spec = TOOLS.get(tool)
    g_map = _global_enabled_map()
    if spec is not None and spec.scope == "container" and container:
        raw_map = await read_container_raw_map(container)
        if raw_map is not None and tool in raw_map:
            return bool(raw_map[tool])
    if tool in g_map:
        return bool(g_map[tool])
    return _new_tool_default()


async def resolve_matrix(containers: list[str]) -> list[dict[str, Any]]:
    """Build the container×tool matrix view for GET /admin/tools.

    For each container returns ``{container, resolved_map, raw_map}`` where
    resolved_map = the effective per-tool switches (container override layered
    over the global map; global-scope tools always reflect the global value) and
    raw_map = the container's own override blob, or None when fully inheriting.
    Never raises — a bad container degrades to inherit-only.
    """
    g_map = _global_enabled_map()
    out: list[dict[str, Any]] = []
    for container in containers:
        raw_map = await read_container_raw_map(container)
        resolved: dict[str, bool] = {}
        for name, spec in TOOLS.items():
            if spec.scope == "global":
                resolved[name] = bool(g_map.get(name, _new_tool_default()))
            elif raw_map is not None and name in raw_map:
                resolved[name] = bool(raw_map[name])
            else:
                resolved[name] = bool(g_map.get(name, _new_tool_default()))
        out.append({"container": container, "resolved_map": resolved, "raw_map": raw_map})
    return out


def list_tools() -> list[dict[str, Any]]:
    """Static descriptors for every preset tool (GET /admin/tools `tools` field)."""
    return [
        {"name": t.name, "scope": t.scope, "description": t.description}
        for t in TOOLS.values()
    ]


# ── Invocation ───────────────────────────────────────────────────────────────


def _result(
    tool: str,
    status: str,
    container: Optional[str],
    result: dict,
    applied: bool,
    notes: str,
) -> dict[str, Any]:
    return {
        "tool": tool,
        "status": status,
        "container": container,
        "result": result,
        "applied": applied,
        "notes": notes,
    }


async def invoke_tool(
    tool: str,
    container: Optional[str] = None,
    params: Optional[dict] = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Invoke a governance tool (POST /admin/tools/{tool}/invoke contract).

    Dispatch:
      * Unknown tool → status='error'.
      * Tool disabled (resolve_tool_enabled false) → status='disabled', no-op.
      * SAFE tool (manage_token_quotas / analyze_retrieval_latency /
        update_container_routing) → run for real (the first two read-only, the
        third an additive config write).
      * LLM / destructive tool → status='dry_run' (preview + plan) when dry_run
        (the endpoint default), else the real handler executes: quarantine
        (reversible snapshot+move) / compress (additive index card) / tune
        (guard-railed config write) — status='applied' on success.

    Never raises — any handler error degrades to status='error' in the result.
    """
    params = params or {}
    spec = TOOLS.get(tool)
    if spec is None:
        return _result(tool, "error", container, {"error": "unknown_tool"}, False,
                       f"no such governance tool: {tool}")
    if not await resolve_tool_enabled(tool, container):
        return _result(tool, "disabled", container, {}, False,
                       "tool disabled by global/container enable map")
    try:
        if tool == "manage_token_quotas":
            return await _invoke_manage_token_quotas(params)
        if tool == "analyze_retrieval_latency":
            return _invoke_analyze_retrieval_latency(container, params)
        if tool == "update_container_routing":
            return await _invoke_update_container_routing(container, params, dry_run)
        # LLM / destructive 工具：dry_run（端点默认）仍只产 plan 预览；显式
        # dry_run=false 才真执行（可逆/附加/护栏语义见各 handler docstring）。
        if dry_run:
            return _invoke_deferred_or_dry_run(spec, container, params, dry_run)
        if tool == "snapshot_and_quarantine":
            return _invoke_snapshot_and_quarantine(container, params)
        if tool == "compress_knowledge_cluster":
            return await _invoke_compress_knowledge_cluster(container, params)
        if tool == "tune_model_parameters":
            return await _invoke_tune_model_parameters(container, params)
        # 注册表里没接真执行 handler 的兜底（当前不可达，防未来新工具漏接线）。
        return _invoke_deferred_or_dry_run(spec, container, params, dry_run)
    except Exception as exc:  # noqa: BLE001 - an invoke must never raise into the endpoint
        logger.warning("[governance_tools] invoke %s degraded: %s", tool, exc)
        return _result(tool, "error", container, {"error": str(exc)}, False,
                       "tool handler degraded")


async def _invoke_manage_token_quotas(params: dict) -> dict[str, Any]:
    """SAFE read: report an agent's token budget vs. live usage (P3 data).

    Reads the configured daily/hourly budgets (config_store) + the live windowed
    totals (token_meter → Redis). No mutation. `mode_suggestion` flags whether the
    agent is at/over either budget (mirrors token_meter.over_budget's logic). The
    agent id is read from params (default the metering DEFAULT_AGENT_ID). Graceful:
    a Redis outage reports usage 0 (fail-open, like the live quota gate)."""
    agent_id = str(params.get("agent_id") or token_meter.DEFAULT_AGENT_ID)
    daily_budget = config_store.get_cached("config:token:daily_budget", None)
    hourly_budget = config_store.get_cached("config:token:hourly_budget", None)
    live = await token_meter.live_today_totals([agent_id])
    daily_used = int(live.get(agent_id, 0))
    over = await token_meter.over_budget(agent_id, str(params.get("model") or ""))
    hourly_used = await _agent_hourly_used(agent_id)
    suggestion = "fallback" if over.get("over") else "normal"
    result = {
        "agent_id": agent_id,
        "daily_used": daily_used,
        "daily_budget": daily_budget,
        "hourly_used": hourly_used,
        "hourly_budget": hourly_budget,
        "mode_suggestion": suggestion,
        "over_budget_scope": over.get("scope"),
        "fallback_model": over.get("fallback_model"),
    }
    return _result("manage_token_quotas", "ok", None, result, False,
                   "read-only token quota / usage report (no mutation)")


async def _agent_hourly_used(agent_id: str) -> int:
    """Live hourly token total for an agent (Redis), 0 on any miss/outage."""
    try:
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        return await token_meter._window_total(  # noqa: SLF001 - reuse P3 windowed read
            token_meter._HOURLY_PREFIX, stamp, agent_id
        )
    except Exception:  # noqa: BLE001 - live overlay best-effort
        return 0


# Retrieval routes whose latency this tool always reports (RAG read path).
_RETRIEVAL_PATHS: tuple[str, ...] = ("/search", "/query")
# Mirrors usage_analytics._WINDOW_SECONDS keys without importing it at module
# scope (usage_analytics pulls fastapi; this module must stay import-light).
_LATENCY_WINDOWS: tuple[str, ...] = ("1h", "24h", "7d", "30d")
_LATENCY_DEFAULT_WINDOW = "7d"
# usage_analytics.endpoints caps limit at 200; route-template cardinality is
# bounded, so 200 always covers /search and /query when they have traffic.
_LATENCY_ENDPOINT_LIMIT = 200
# The usage log aggregation has no per-container latency cut, so granularity
# stays global regardless of the requested container.
_LATENCY_GRANULARITY_NOTE = (
    "granularity=global: usage analytics aggregates per endpoint, not per "
    "container — container filter not applied"
)


def _workspace_root() -> Path:
    # Read env at call time (not import) so tests / redeploys can repoint it.
    return Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))


def _usage_db_path() -> Path:
    # Same convention as the server's _queue_db_path; not imported from there
    # because task_rag_server imports this module (would be circular).
    return _workspace_root() / "tasks" / "rag" / "queue.db"


def _usage_endpoint_rows(db_path: Path, window: str) -> list[dict[str, Any]]:
    """Per-endpoint calls/p50/p95 via the SAME aggregation /admin/usage/endpoints
    serves (usage_analytics.endpoints) — no parallel SQL to drift from it."""
    try:
        try:
            import usage_analytics  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts import usage_analytics  # type: ignore
    except ImportError:  # pragma: no cover - slim env without fastapi → no datasource
        return []
    data = usage_analytics.endpoints(
        db_path, window=window, sort="calls", limit=_LATENCY_ENDPOINT_LIMIT
    )
    return list(data.get("rows") or [])


def _invoke_analyze_retrieval_latency(
    container: Optional[str], params: dict
) -> dict[str, Any]:
    """SAFE read: retrieval latency report from the live usage analytics log.

    Reads the per-endpoint aggregation backing GET /admin/usage/endpoints and
    projects the retrieval routes (/search, /query) to calls/p50_ms/p95_ms.
    Read-only: when the usage DB does not exist yet it reports ok + empty
    metrics instead of creating the file. invoke_tool wraps any handler error."""
    window = str(params.get("window") or _LATENCY_DEFAULT_WINDOW)
    if window not in _LATENCY_WINDOWS:
        window = _LATENCY_DEFAULT_WINDOW
    metrics: dict[str, Any] = {"window": window, "granularity": "global", "endpoints": {}}
    db_path = _usage_db_path()
    if not db_path.exists():
        return _result(
            "analyze_retrieval_latency", "ok", container,
            {"latency_metrics": metrics}, False,
            "no usage data recorded yet — empty metrics; " + _LATENCY_GRANULARITY_NOTE,
        )
    by_path = {r.get("path"): r for r in _usage_endpoint_rows(db_path, window)}
    for path in _RETRIEVAL_PATHS:
        row = by_path.get(path) or {}
        metrics["endpoints"][path] = {
            "calls": int(row.get("calls") or 0),
            "p50_ms": int(row.get("p50_latency_ms") or 0),
            "p95_ms": int(row.get("p95_latency_ms") or 0),
        }
    return _result(
        "analyze_retrieval_latency", "ok", container,
        {"latency_metrics": metrics}, False,
        "read-only retrieval latency report from usage analytics; "
        + _LATENCY_GRANULARITY_NOTE,
    )


async def _invoke_update_container_routing(
    container: Optional[str], params: dict, dry_run: bool = False
) -> dict[str, Any]:
    """SAFE write: additively merge this container's routing entry into
    ``config:container:routing_rules`` via config_store.set (the SAME path PUT
    /admin/config uses — no write bypass).

    The merge is additive: it reads the current routing_rules json, updates ONLY
    the target container's key, and writes the whole blob back — other containers'
    entries are preserved. Either the top-level dry_run (endpoint contract default
    True) OR params.dry_run previews the merged blob without writing — a write
    happens only when BOTH say "not a dry run" (OR semantics = the conservative
    side wins, so the endpoint default never silently persists). A missing
    container / rules param → status='error'. Never raises."""
    if not container:
        return _result("update_container_routing", "error", container, {}, False,
                       "container is required for routing update")
    rules = params.get("rules")
    if not isinstance(rules, dict):
        return _result("update_container_routing", "error", container, {}, False,
                       "params.rules (object) is required")
    current = config_store.get_cached("config:container:routing_rules", {})
    merged = dict(current) if isinstance(current, dict) else {}
    merged[container] = rules  # additive: only this container's entry changes
    effective_dry_run = dry_run or bool(params.get("dry_run", False))
    if effective_dry_run:
        return _result("update_container_routing", "dry_run", container,
                       {"merged_routing_rules": merged}, False,
                       "preview only — routing_rules not written (dry_run)")
    ok = await config_store.set("config:container:routing_rules", merged)
    status = "ok" if ok else "error"
    return _result(
        "update_container_routing", status, container,
        {"merged_routing_rules": merged if ok else None},
        ok,
        "routing_rules merged + persisted via config_store.set"
        if ok else "config_store.set failed (persist error)",
    )


# Quarantine candidate heuristics — simple, but computed from real rows.
_QUARANTINE_MAX_AGE_DAYS = 180   # no update/access signal for this long → stale
_QUARANTINE_MIN_TEXT_CHARS = 20  # shorter content carries too little signal
_QUARANTINE_MAX_CANDIDATES = 50  # bound the plan payload; scanned_count stays total
_SECONDS_PER_DAY = 86_400


def _memory_objects_file(container: str) -> Path:
    # Same layout as task_rag_runtime.container_dir — duplicated here because
    # that helper mkdirs (a write) and this scan must stay strictly read-only.
    return (
        _workspace_root() / "tasks" / "rag" / "containers" / container
        / "memory_objects.jsonl"
    )


def _row_last_updated(row: dict) -> Optional[int]:
    """Newest epoch-seconds stamp on a memory row (same keys list_memories uses)."""
    stamps = [
        int(row[k]) for k in ("updatedAt", "createdAt", "storedAt")
        if isinstance(row.get(k), (int, float))
    ]
    return max(stamps) if stamps else None


def _candidate_from_row(
    row: dict, raw_line: str, now: float, max_age_days: int
) -> Optional[dict[str, Any]]:
    """Low-value heuristics on one real row → candidate dict, or None (healthy)."""
    last_updated = _row_last_updated(row)
    text = row.get("text") if isinstance(row.get("text"), str) else ""
    reasons: list[str] = []
    if last_updated is not None and (now - last_updated) > max_age_days * _SECONDS_PER_DAY:
        reasons.append(f"stale: last update older than {max_age_days}d")
    if len(text.strip()) < _QUARANTINE_MIN_TEXT_CHARS:
        reasons.append(f"short content: text under {_QUARANTINE_MIN_TEXT_CHARS} chars")
    if not reasons:
        return None
    return {
        "document_id": str(row.get("id") or "") or None,
        "bytes": len(raw_line.encode("utf-8")),
        "last_updated": last_updated,
        "reasons": reasons,
    }


def _scan_quarantine_candidates(
    container: Optional[str], params: dict
) -> dict[str, Any]:
    """Read-only scan of a container's memory_objects.jsonl for low-value rows.

    Feeds the snapshot_and_quarantine plan with real data; never writes, never
    raises (a missing/unreadable store degrades to an empty scan + note)."""
    if not container:
        return {"scanned_count": 0, "candidates": [],
                "scan_notes": "container required for candidate scan"}
    path = _memory_objects_file(container)
    if not path.exists():
        return {"scanned_count": 0, "candidates": [],
                "scan_notes": "container has no memory_objects.jsonl — nothing to scan"}
    max_age_days = int(params.get("max_age_days") or _QUARANTINE_MAX_AGE_DAYS)
    now = time.time()
    scanned = 0
    candidates: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                row = _parse_memory_line(line)
                if row is None:
                    continue
                scanned += 1
                cand = _candidate_from_row(row, line, now, max_age_days)
                if cand is not None:
                    candidates.append(cand)
    except OSError as exc:
        return {"scanned_count": scanned, "candidates": [],
                "scan_notes": f"scan degraded (store unreadable: {exc})"}
    notes = f"scanned {scanned} memory rows (read-only)"
    if len(candidates) > _QUARANTINE_MAX_CANDIDATES:
        notes += (f"; candidates truncated to first {_QUARANTINE_MAX_CANDIDATES} "
                  f"of {len(candidates)}")
        candidates = candidates[:_QUARANTINE_MAX_CANDIDATES]
    return {"scanned_count": scanned, "candidates": candidates, "scan_notes": notes}


def _parse_memory_line(line: str) -> Optional[dict]:
    """One JSONL line → dict row; blank / corrupt lines → None (skip, don't fail)."""
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    return row if isinstance(row, dict) else None


def _scan_compress_preview(container: Optional[str], params: dict) -> dict[str, Any]:
    """Read-only：预览 compress 会聚类哪个簇 + 簇内来源（不调 LLM，永不抛）。

    让 compress 的 dry_run 像 quarantine 一样是可执行预览而非参数回声——
    复用真执行同一 _pick_cluster 启发式，所见即所得。"""
    if not container:
        return {"cluster_tag": None, "cluster_size": 0, "source_ids": [],
                "would_compress": False,
                "scan_notes": "container required for cluster preview"}
    try:
        rows = _read_memory_rows(container)
    except OSError as exc:
        return {"cluster_tag": None, "cluster_size": 0, "source_ids": [],
                "would_compress": False,
                "scan_notes": f"preview degraded (store unreadable: {exc})"}
    cluster_tag, cluster = _pick_cluster(rows, params.get("cluster_tag"))
    would = len(cluster) >= 2
    # 字节账目（不调 LLM）：用真执行同一分批启发式估算簇全文字节 + 批数，
    # 让预览能预告 map-reduce 会切几批，便于提前发现超大簇。
    batch_byte_budget = _compress_batch_bytes()
    row_char_cap = _compress_row_char_cap()
    estimated_bytes = sum(
        len(_format_cluster_row(r, row_char_cap).encode("utf-8")) for r in cluster
    )
    batches = _batch_cluster_by_bytes(cluster, batch_byte_budget, row_char_cap) \
        if would else []
    batch_count = len(batches)
    if would:
        mb = batch_byte_budget / (1024 * 1024)
        scan_notes = (
            f"largest same-tags cluster has {len(cluster)} rows "
            f"(read-only; would map-reduce in {batch_count} batch(es) "
            f"≤{mb:.0f}MiB each to one index card)")
    else:
        scan_notes = "no cluster ≥2 — nothing to compress"
    return {
        "cluster_tag": cluster_tag or None,
        "cluster_size": len(cluster),
        "source_ids": [str(r.get("id") or "") for r in cluster],
        "would_compress": would,
        "estimated_bytes": estimated_bytes,
        "batch_count": batch_count,
        "scan_notes": scan_notes,
    }


def _scan_tune_preview(container: Optional[str], params: dict) -> dict[str, Any]:
    """Read-only：预览 tune 会喂给网关 LLM 的信号 + 当前参数 + 护栏（不调 LLM，
    永不抛）。让 tune 的 dry_run 展示「将依据什么调参、护栏边界」而非空回声。"""
    try:
        latency = _invoke_analyze_retrieval_latency(container, {})
        object_count = len(_read_memory_rows(container)) if container else 0
        before = {k: config_store.get_cached(cfg, None)
                  for k, cfg in _TUNE_CONFIG_KEYS.items()}
    except Exception as exc:  # noqa: BLE001 - preview 必须 graceful
        return {"signals": None, "tunable_keys": list(_TUNE_ALLOWED_KEYS),
                "scan_notes": f"preview degraded: {exc}"}
    return {
        "signals": {
            "latency_metrics": latency.get("result", {}).get("latency_metrics"),
            "object_count": object_count,
            "current_config": before,
        },
        "tunable_keys": list(_TUNE_ALLOWED_KEYS),
        "guardrails": {
            "similarity_threshold": "float in [0,1]",
            "default_topk": "int in [1,50]",
            "citation_enabled": "bool",
        },
        "scan_notes": ("would feed these signals to the gateway LLM; only "
                       "allow-listed keys within range get written"),
    }


def _invoke_deferred_or_dry_run(
    spec: _Tool, container: Optional[str], params: dict, dry_run: bool
) -> dict[str, Any]:
    """LLM / destructive 工具的 plan 预览（不动数据）。

    dry_run=True（端点默认）→ status='dry_run'，产真实预览 plan（would_execute
    恒 False = 「本次不执行」），notes 指引传 dry_run=false 真执行。三个工具的
    plan 都额外携带目标容器真实存储的只读扫描（quarantine 候选 / compress 目标簇 /
    tune 信号+护栏），让预览可执行而非参数回声。dry_run=False 仅作未接线工具的
    兜底（当前注册表三个工具都已有真执行 handler，正常不可达）→ status='deferred'。"""
    kind = "needs LLM (routes via rag_engine gateway, HR-9)" if spec.needs_llm \
        else "destructive (reversible snapshot + quarantine)"
    plan = {
        "tool": spec.name,
        "container": container,
        "would_execute": False,
        "reason_not_executed": "dry_run preview; pass dry_run=false to execute",
        "params_echo": params,
    }
    if spec.name == "snapshot_and_quarantine":
        plan.update(_scan_quarantine_candidates(container, params))
    elif spec.name == "compress_knowledge_cluster":
        plan.update(_scan_compress_preview(container, params))
    elif spec.name == "tune_model_parameters":
        plan.update(_scan_tune_preview(container, params))
    if dry_run:
        return _result(spec.name, "dry_run", container, {"plan": plan}, False,
                       f"dry_run preview ({kind}) — pass dry_run=false to execute")
    return _result(spec.name, "deferred", container, {"plan": plan}, False,
                   f"deferred — no real-execution handler wired for {spec.name}")


# ── Real execution (dry_run=false) — shared plumbing ─────────────────────────


# 索引卡压缩的 system prompt：高密度+模糊召回友好（标题同义词头部）是
# founder 自然语言检索习惯的硬要求，故固化在 prompt 里而非交给调用方。
_INDEX_CARD_SYSTEM_PROMPT = (
    "把以下同主题记忆压缩成一张高密度索引卡，中文，不超过约 400 字，必须包含："
    "①自然语言同义词头部（便于模糊召回）②核心结论 bullet 列表③来源 id 列表。"
)
# map-reduce 的 reduce 阶段 system prompt：把多张「局部索引卡」合并为一张最终卡。
# 当簇过大（拼成的单条 prompt 超上游网关请求体上限）时，按字节预算分批 map
# 出多张局部卡，再用本 prompt reduce 合并——同样保高密度+模糊召回友好头部。
_INDEX_CARD_REDUCE_SYSTEM_PROMPT = (
    "以下是同一主题记忆分批压缩得到的多张局部索引卡。请把它们合并为一张"
    "高密度索引卡，中文，不超过约 400 字，必须包含：①自然语言同义词头部"
    "（便于模糊召回）②去重后的核心结论 bullet 列表③合并所有来源 id 列表。"
    "不要丢失任一局部卡的关键结论。"
)
# tune_model_parameters 的 LLM 输出契约：严格只回 JSON，便于机器解析+护栏校验。
_TUNE_SYSTEM_PROMPT = (
    "你是 RAG 检索参数调优器。根据给定的检索时延/规模/当前参数信号，"
    "严格只输出一个 JSON 对象（无 markdown 包裹、无解释文字）："
    '{"similarity_threshold": float|null, "default_topk": int|null, '
    '"citation_enabled": bool|null, "rationale": str}。null 表示不调整该项。'
)
# 调参护栏：allow-list（仅这 3 个键可写）+ 建议键 → config_store 注册键映射。
_TUNE_ALLOWED_KEYS: tuple[str, ...] = (
    "similarity_threshold", "default_topk", "citation_enabled",
)
_TUNE_CONFIG_KEYS: dict[str, str] = {
    "similarity_threshold": "config:rag:similarity_threshold",
    "default_topk": "config:rag:default_topk",
    "citation_enabled": "config:rag:citation_enabled",
}


async def _llm_oneshot(prompt: str, system_prompt: Optional[str] = None) -> str:
    """一次性 LLM 调用 —— 经 rag_engine 的 env (`LLM_*`) 网关入口（HR-9 唯一
    许可通道，自带重试 + token 计量）。任何 import/网络/模型异常都收敛为
    RuntimeError（invoke_tool 兜底降级为 status='error'，绝不 raise 进 endpoint）。"""
    try:
        try:
            import rag_engine  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts import rag_engine  # type: ignore
        return str(await rag_engine._llm_func(prompt, system_prompt=system_prompt))  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - 统一收敛，不让网关细节外溢
        raise RuntimeError(f"llm call failed: {exc}") from exc


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> Path:
    """原子 JSONL 写（tmp + rename）。镜像 server 端 write_memory_objects 的
    工艺；自包含实现是因为本模块不可反向 import task_rag_server（会循环）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp_path.replace(path)
    return path


def _governance_dir(container: str) -> Path:
    """治理 sidecar 目录（快照/隔离文件落点）——与记忆主文件同容器目录隔离存放。"""
    return _memory_objects_file(container).parent / "governance"


def _read_memory_rows(container: str) -> list[dict]:
    """读取容器 memory_objects.jsonl 全量行（坏行/空行跳过）；缺文件 → 空列表。"""
    path = _memory_objects_file(container)
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            row = _parse_memory_line(line)
            if row is not None:
                rows.append(row)
    return rows


# ── Real execution: snapshot_and_quarantine（可逆） ──────────────────────────


def _invoke_snapshot_and_quarantine(
    container: Optional[str], params: dict
) -> dict[str, Any]:
    """真执行（可逆）：全量快照 → 候选移入隔离文件 → 幸存行原子重写主文件。

    零硬删：候选行只是搬进 governance/quarantine-<ts>.jsonl，全量快照
    snapshot-<ts>.jsonl 同时留存 —— 任何时刻可用快照整体回滚。候选判定复用
    dry_run plan 的同一启发式（_candidate_from_row），预览与执行不会分叉。
    候选为空时不动主文件（applied=True 但 quarantined_count=0）。"""
    if not container:
        return _result("snapshot_and_quarantine", "error", container, {}, False,
                       "container is required for quarantine execution")
    rows = _read_memory_rows(container)
    max_age_days = int(params.get("max_age_days") or _QUARANTINE_MAX_AGE_DAYS)
    now = time.time()
    quarantined: list[dict] = []
    survivors: list[dict] = []
    for row in rows:
        raw = json.dumps(row, ensure_ascii=False) + "\n"
        if _candidate_from_row(row, raw, now, max_age_days) is None:
            survivors.append(row)
        else:
            quarantined.append(row)
    ts = int(time.time())
    gov_dir = _governance_dir(container)
    snapshot_path = _atomic_write_jsonl(gov_dir / f"snapshot-{ts}.jsonl", rows)
    quarantine_path = _atomic_write_jsonl(gov_dir / f"quarantine-{ts}.jsonl", quarantined)
    if quarantined:  # 候选为空就不碰主文件，避免无意义的重写+重建
        _atomic_write_jsonl(_memory_objects_file(container), survivors)
    result = {
        "snapshot_path": str(snapshot_path),
        "quarantine_path": str(quarantine_path),
        "quarantined_count": len(quarantined),
        "quarantined_ids": [str(r.get("id") or "") for r in quarantined],
        "survivors_count": len(survivors),
        "reindex_required": bool(quarantined),
        "reversible": True,
    }
    return _result(
        "snapshot_and_quarantine", "applied", container, result, True,
        f"quarantined {len(quarantined)}/{len(rows)} rows "
        "(full snapshot retained — reversible, no hard delete)",
    )


# ── Real execution: compress_knowledge_cluster（LLM 索引卡，附加式） ──────────

# 字节预算护栏：簇全文拼成单条 prompt 曾撑到 ~12.76 MB，越过上游网关 10 MiB transport
# 输入硬限 → 400；但真正更紧的约束是网关小模型的 token context window —— prompt 字节
# 经 tokenizer 变 token，纯 ASCII 约 4 字节/token、CJK UTF-8 约 1.5 字节/token（3 字节/
# 字节是 token 的弱代理：CJK UTF-8 3 字节/字符 ≈ 1 token/字符，比 ASCII 更费 token；
# 用重复字符（如 'x'）探测网关 context 上限会严重高估真实/CJK 内容能塞的字节数。
# 真实内容实测（混合 CJK+Latin acc-demo bigcluster 直探 gpt-5.4-mini via 网关）：
#   768 KiB（786432 B）→ 200 OK
#   1 MiB（1048576 B）→ HTTP 400 code=context_too_large
# 叠加 compress 自身 _INDEX_CARD_SYSTEM_PROMPT + 每行格式开销 + 输出 token 预留，
# 安全默认须明显低于 768 KiB。取 256 KiB（262144 B）—— 对实测混合内容 ~3× 余量，
# 对更密的纯 CJK 内容同样安全。治理操作低频，大簇多分几批可接受。
# 阈值经 config_store 可调（接大 context 模型可经 TM_COMPRESS_BATCH_BYTES 调高）。
_COMPRESS_BATCH_BYTES_DEFAULT = 256 * 1024
_COMPRESS_ROW_CHAR_CAP_DEFAULT = 20000


def _truncate_for_llm(text: str, byte_budget: int) -> str:
    """UTF-8 安全地把 text 截到 byte_budget 字节内；超限时尾部补
    " …[truncated N bytes]" 标记。byte_budget<=0 或非字符串 → 返回空串/原值的
    保守处理。与 agent 循环「工具结果回灌下一轮 prompt」共用，确保任何超大工具
    输出都不会在循环里重演请求体超限。永不抛。"""
    if not isinstance(text, str):
        text = str(text)
    if byte_budget <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= byte_budget:
        return text
    dropped = len(raw) - byte_budget
    marker = f" …[truncated {dropped} bytes]"
    # 给 marker 留出空间，再在 byte_budget 内做不切断多字节字符的截断。
    keep_budget = max(0, byte_budget - len(marker.encode("utf-8")))
    head = raw[:keep_budget].decode("utf-8", errors="ignore")
    return head + marker


def _compress_batch_bytes() -> int:
    """每批 prompt 的 UTF-8 字节上限。env TM_COMPRESS_BATCH_BYTES 优先（非法忽略），
    其次 config:agent:compress_batch_bytes，再退默认 256 KiB（context-safe，见上方常量注释）。
    永不抛。"""
    env_raw = os.environ.get("TM_COMPRESS_BATCH_BYTES")
    if env_raw:
        try:
            val = int(env_raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    try:
        val = int(config_store.get_cached(
            "config:agent:compress_batch_bytes", _COMPRESS_BATCH_BYTES_DEFAULT))
        return val if val > 0 else _COMPRESS_BATCH_BYTES_DEFAULT
    except (TypeError, ValueError):
        return _COMPRESS_BATCH_BYTES_DEFAULT


def _compress_row_char_cap() -> int:
    """单行 text 截断的字符上限。config:agent:compress_row_char_cap，退默认 20000。
    永不抛。"""
    try:
        val = int(config_store.get_cached(
            "config:agent:compress_row_char_cap", _COMPRESS_ROW_CHAR_CAP_DEFAULT))
        return val if val > 0 else _COMPRESS_ROW_CHAR_CAP_DEFAULT
    except (TypeError, ValueError):
        return _COMPRESS_ROW_CHAR_CAP_DEFAULT


def _format_cluster_row(row: dict, row_char_cap: int) -> str:
    """把一条记忆行格式化为 prompt 片段，text 先截到 row_char_cap 字符。"""
    text = str(row.get("text") or "")
    if len(text) > row_char_cap:
        text = text[:row_char_cap] + "…"
    return f"[{row.get('id')}] {row.get('title') or ''}\n{text}"


def _batch_cluster_by_bytes(
    cluster: list[dict], batch_byte_budget: int, row_char_cap: int
) -> list[list[dict]]:
    """按 UTF-8 字节贪心 bin-packing 把簇切成多批，使每批拼出的 prompt
    （含 system prompt 开销）控在 batch_byte_budget 内。

    - 先对每行 text 截到 row_char_cap（防单条超大记忆撑爆一批）再计字节。
    - 行分隔符 "\\n\\n" 一并计入。单条行本身已超预算（极端）→ 仍单独成一批；
      map 步送 LLM 前会用 _truncate_for_llm(batch_prompt, batch_byte_budget) 再兜底
      硬截到预算内，故永不空批死循环、永不触发上游 400。
    - 永不抛；预算非正时退守每行一批的保守切法。"""
    if batch_byte_budget <= 0:
        batch_byte_budget = _COMPRESS_BATCH_BYTES_DEFAULT
    # 预留给 system prompt + 拼装开销的固定头寸（取两张 system prompt 的较大者）。
    system_overhead = max(
        len(_INDEX_CARD_SYSTEM_PROMPT.encode("utf-8")),
        len(_INDEX_CARD_REDUCE_SYSTEM_PROMPT.encode("utf-8")),
    )
    effective_budget = max(1, batch_byte_budget - system_overhead)
    sep_bytes = len("\n\n".encode("utf-8"))
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_bytes = 0
    for row in cluster:
        frag = _format_cluster_row(row, row_char_cap)
        frag_bytes = len(frag.encode("utf-8"))
        add_bytes = frag_bytes + (sep_bytes if current else 0)
        if current and current_bytes + add_bytes > effective_budget:
            batches.append(current)
            current = [row]
            current_bytes = frag_bytes
        else:
            current.append(row)
            current_bytes += add_bytes
    if current:
        batches.append(current)
    return batches


def _pick_cluster(rows: list[dict], cluster_tag: Any) -> tuple[str, list[dict]]:
    """选簇：params.cluster_tag 指定时取含该 tag 的行；否则按「相同 tags 集合」
    聚簇取最大簇（同 tag 集 = 同主题的最强信号，无需 LLM 预判）。"""
    if cluster_tag:
        tag = str(cluster_tag)
        return tag, [
            r for r in rows
            if isinstance(r.get("tags"), list) and tag in [str(t) for t in r["tags"]]
        ]
    clusters: dict[tuple[str, ...], list[dict]] = {}
    for row in rows:
        tags = row.get("tags")
        if isinstance(tags, list) and tags:
            sig = tuple(sorted({str(t) for t in tags}))
            clusters.setdefault(sig, []).append(row)
    if not clusters:
        return "", []
    sig, cluster = max(clusters.items(), key=lambda kv: len(kv[1]))
    return ",".join(sig), cluster


async def _invoke_compress_knowledge_cluster(
    container: Optional[str], params: dict
) -> dict[str, Any]:
    """真执行（附加式）：同主题（同 tags 簇）记忆经网关 LLM 压缩成一张高密度
    索引卡，作为**新行** append 进 memory_objects.jsonl —— 源记忆零删除，
    回滚 = 删掉这一行新卡。簇 <2 不压缩（单条无聚合价值，省一次 LLM 调用）。"""
    if not container:
        return _result("compress_knowledge_cluster", "error", container, {}, False,
                       "container is required for cluster compression")
    rows = _read_memory_rows(container)
    cluster_tag, cluster = _pick_cluster(rows, params.get("cluster_tag"))
    if len(cluster) < 2:
        return _result(
            "compress_knowledge_cluster", "ok", container,
            {"cluster_tag": cluster_tag, "cluster_size": len(cluster)}, False,
            "no cluster ≥2 — nothing to compress",
        )
    # map-reduce 字节预算分批：把簇切到每批 prompt < batch_byte_budget（UTF-8），
    # 防整簇拼成单条 prompt 越过上游网关 10 MiB 输入硬限触发 400。
    batch_byte_budget = _compress_batch_bytes()
    row_char_cap = _compress_row_char_cap()
    batches = _batch_cluster_by_bytes(cluster, batch_byte_budget, row_char_cap)
    # map：每批单独压缩成一张局部索引卡。送 LLM 前再用 _truncate_for_llm 把整批
    # prompt 硬截到 batch_byte_budget（< 10 MiB 上游硬限）做 belt-and-suspenders 兜底
    # ——即便 row_char_cap 被调到很大、bin-packing 仍让单批越界，也永不触发 400。
    # 正常情况下批本就 < 预算，截断为 no-op。
    partial_cards: list[str] = []
    for batch in batches:
        batch_prompt = "\n\n".join(
            _format_cluster_row(r, row_char_cap) for r in batch
        )
        batch_prompt = _truncate_for_llm(batch_prompt, batch_byte_budget)
        partial_cards.append(
            await _llm_oneshot(batch_prompt, system_prompt=_INDEX_CARD_SYSTEM_PROMPT)
        )
    # reduce：>1 批时合并局部卡为最终卡；==1 批直接用该批输出（廉价单跳路径）。
    if len(partial_cards) > 1:
        reduce_prompt = "\n\n".join(
            f"[局部卡 {i + 1}]\n{c}" for i, c in enumerate(partial_cards)
        )
        card_text = await _llm_oneshot(
            reduce_prompt, system_prompt=_INDEX_CARD_REDUCE_SYSTEM_PROMPT)
    else:
        card_text = partial_cards[0]
    ts = int(time.time())
    sig = hashlib.md5(cluster_tag.encode("utf-8")).hexdigest()[:8]
    source_ids = [str(r.get("id") or "") for r in cluster]
    tags = sorted({
        str(t) for r in cluster for t in (r.get("tags") or []) if str(t) != "index-card"
    })
    card = {
        "id": f"idxcard-{sig}-{ts}",
        "title": f"[索引卡] {cluster_tag}",
        "text": card_text,
        "tags": [*tags, "index-card"],
        "createdAt": ts,
        "metadata": {
            "source_ids": source_ids,
            "generated_by": "compress_knowledge_cluster",
            "llm_model": os.environ.get("LLM_MODEL"),
        },
    }
    _atomic_write_jsonl(_memory_objects_file(container), [*rows, card])
    result = {
        "card_id": card["id"],
        "source_ids": source_ids,
        "cluster_tag": cluster_tag,
        "cluster_size": len(cluster),
        "batch_count": len(batches),
        "llm_model": os.environ.get("LLM_MODEL"),
        "reindex_required": True,
    }
    reduce_note = (f" via {len(batches)}-batch map-reduce"
                   if len(batches) > 1 else "")
    return _result(
        "compress_knowledge_cluster", "applied", container, result, True,
        f"index card appended from {len(cluster)} source rows"
        f"{reduce_note} (sources kept)",
    )


# ── Real execution: tune_model_parameters（LLM 调参带护栏） ──────────────────


def _validate_tune_value(key: str, value: Any) -> tuple[bool, Any]:
    """护栏：逐键类型+范围校验（bool 是 int 子类，须先排除）。返回 (通过?, 规范值)。"""
    if key == "similarity_threshold":
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and 0.0 <= float(value) <= 1.0)
        return ok, (float(value) if ok else None)
    if key == "default_topk":
        ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 50
        return ok, (value if ok else None)
    if key == "citation_enabled":
        return isinstance(value, bool), value
    return False, None


async def _apply_tune_suggestion(
    suggestion: dict, before: dict
) -> tuple[list[str], list[dict], dict]:
    """护栏过滤 + 应用：allow-list 内逐键校验，越界/类型不符丢弃记 rejected，
    通过的经 config_store.set 写入（与 PUT /admin/config 同一写路径，无旁路）。
    返回 (applied_keys, rejected, after)。"""
    applied_keys: list[str] = []
    rejected: list[dict] = []
    after = dict(before)
    for key in _TUNE_ALLOWED_KEYS:
        value = suggestion.get(key)
        if value is None:  # null = LLM 明示不调整该项
            continue
        ok, normalized = _validate_tune_value(key, value)
        if not ok:
            rejected.append({"key": key, "value": value,
                             "reason": "out_of_range_or_bad_type"})
        elif await config_store.set(_TUNE_CONFIG_KEYS[key], normalized):
            applied_keys.append(key)
            after[key] = normalized
        else:
            rejected.append({"key": key, "value": value,
                             "reason": "config_store_set_failed"})
    for k in sorted(set(suggestion) - {*_TUNE_ALLOWED_KEYS, "rationale"}):
        rejected.append({"key": k, "value": suggestion[k],
                         "reason": "not_in_allow_list"})
    return applied_keys, rejected, after


async def _invoke_tune_model_parameters(
    container: Optional[str], params: dict
) -> dict[str, Any]:
    """真执行（带护栏）：采集时延/规模/当前参数信号喂网关 LLM → 严格 JSON
    调参建议 → allow-list + 范围护栏过滤 → 仅把通过校验的键经 config_store
    加性写入。可逆：PUT /admin/config 把对应键置 null 即回退。解析失败 →
    status='error' 且 config 零改动。"""
    latency = _invoke_analyze_retrieval_latency(container, {})
    object_count = len(_read_memory_rows(container)) if container else 0
    before = {k: config_store.get_cached(cfg, None)
              for k, cfg in _TUNE_CONFIG_KEYS.items()}
    prompt = json.dumps({
        "latency_metrics": latency.get("result", {}).get("latency_metrics"),
        "object_count": object_count,
        "current_config": before,
    }, ensure_ascii=False)
    raw = await _llm_oneshot(prompt, system_prompt=_TUNE_SYSTEM_PROMPT)
    try:
        suggestion = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _result("tune_model_parameters", "error", container,
                       {"error": "llm_output_not_json", "raw_head": str(raw)[:200]},
                       False, "LLM output was not valid JSON — config untouched")
    if not isinstance(suggestion, dict):
        return _result("tune_model_parameters", "error", container,
                       {"error": "llm_output_not_object"}, False,
                       "LLM output was not a JSON object — config untouched")
    applied_keys, rejected, after = await _apply_tune_suggestion(suggestion, before)
    applied = bool(applied_keys)
    result = {
        "before": before,
        "after": after,
        "applied_keys": applied_keys,
        "rejected": rejected,
        "rationale": str(suggestion.get("rationale") or ""),
        "llm_model": os.environ.get("LLM_MODEL"),
    }
    return _result(
        "tune_model_parameters", "applied" if applied else "ok", container,
        result, applied,
        "tuned config keys applied via config_store (revert: clear the override)"
        if applied else "no valid tuning suggestion applied — config untouched",
    )
