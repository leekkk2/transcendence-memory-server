#!/usr/bin/env python3
"""Governance toolbox — tool registry + container×tool activation (blueprint P6, §A8).

This module is the **safe, opt-in governance action layer** that sits beside the
dreaming engine. It exposes six preset governance tools (§9.A), resolves whether
each is enabled for a given container (container-level override layered over the
global enable map), and invokes the *safe* ones for real while leaving the
LLM / destructive ones report-only (``dry_run`` / ``deferred``) until an explicit
config switch is flipped — which P6 ships OFF.

Why this is behavior-preserving on a fresh deploy:

  * **Read-only resolution + safe actions only.** ``resolve_*`` are pure config
    reads. ``invoke_tool`` only mutates state for the three SAFE tools
    (manage_token_quotas = read-only; analyze_retrieval_latency = read-only;
    update_container_routing = an additive config write through the SAME
    ``config_store.set`` the PUT /admin/config endpoint uses — no write bypass).
    The LLM / destructive tools never touch data in P6 (status='dry_run' /
    'deferred', applied=False).
  * **No RAG-path readers.** Nothing here is consulted by ``/search`` / ``/query``;
    registering / invoking a tool cannot change retrieval output.
  * **Structural RAG immunity.** Any governance artifact this layer would persist
    goes to the isolated ``governance_store`` (physically separate from user
    corpora) — there is no path from a tool write into a user container's LanceDB.

Invariants (mirror redis_client.py P0 / config_store.py P1 / dreaming.py P6):

  * **Import-safe** — importing opens no connection, touches no network.
  * **Graceful** — every Redis/config/DB read degrades to the conservative
    default (global map → preset defaults); nothing here raises into a caller.
  * **No direct LLM** (HR-9) — compress_knowledge_cluster's real clustering, if
    ever enabled, MUST route through rag_engine's gateway; P6 leaves it deferred
    and calls no model.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import json
import logging
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
# Each tool's static descriptor. `destructive` / `needs_llm` decide whether
# invoke_tool may execute it for real in P6 (only safe = non-destructive,
# non-LLM tools run; the rest are dry_run / deferred). `scope='global'` tools
# ignore the container dimension (manage_token_quotas).


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
        "聚类压缩同主题记忆为高密度索引卡（需 LLM，经 rag_engine 网关）。",
        needs_llm=True,
    ),
    "update_container_routing": _Tool(
        "update_container_routing", "container",
        "更新容器路由规则（写 config:container:routing_rules，经 config_store 加性合并）。",
    ),
    "snapshot_and_quarantine": _Tool(
        "snapshot_and_quarantine", "container",
        "快照并隔离低价值/异常记忆（破坏性，默认仅产计划不执行）。",
        destructive=True,
    ),
    "tune_model_parameters": _Tool(
        "tune_model_parameters", "container",
        "依检索质量调参（需 LLM 评估，默认仅产计划）。",
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
      * LLM / destructive tool → status='dry_run' (preview + plan) when dry_run,
        else 'deferred' (P6 never executes these; the guarding config switch is
        off) — applied=False either way.

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
            return _invoke_analyze_retrieval_latency(container)
        if tool == "update_container_routing":
            return await _invoke_update_container_routing(container, params, dry_run)
        # LLM / destructive tools — report-only in P6 (see module docstring).
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


def _invoke_analyze_retrieval_latency(container: Optional[str]) -> dict[str, Any]:
    """SAFE read: retrieval latency report.

    P6 has no governance-side latency datasource wired into this tool, so it
    answers honestly: status='deferred' + a 'no latency metrics wired' summary
    rather than fabricating numbers. (The per-endpoint p95 lives in
    usage_analytics for the dashboard; surfacing it through this tool is a
    follow-up.) No mutation; never raises."""
    return _result(
        "analyze_retrieval_latency", "deferred", container,
        {"latency_metrics": None},
        False,
        "no latency metrics wired into this tool yet (usage_analytics p95 is a "
        "follow-up); reported honestly rather than fabricated",
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


def _invoke_deferred_or_dry_run(
    spec: _Tool, container: Optional[str], params: dict, dry_run: bool
) -> dict[str, Any]:
    """LLM / destructive tool handler — report-only in P6.

    Returns a plan/preview (status='dry_run') when dry_run is True, else
    'deferred' (the real execution is gated behind a config switch P6 ships off,
    so it is intentionally NOT performed here). applied is always False; no data
    is touched. The plan names the guarding switch and the HR-9 / governance
    constraints so a future phase can wire the real action safely."""
    reason = "needs LLM (route via rag_engine gateway, HR-9)" if spec.needs_llm \
        else "destructive (gated behind an explicit apply switch)"
    plan = {
        "tool": spec.name,
        "container": container,
        "would_execute": False,
        "reason_not_executed": reason,
        "params_echo": params,
    }
    if dry_run:
        return _result(spec.name, "dry_run", container, {"plan": plan}, False,
                       f"dry_run plan only — {reason}; P6 does not execute")
    return _result(spec.name, "deferred", container, {"plan": plan}, False,
                   f"deferred — {reason}; P6 ships the apply switch OFF (followup)")
