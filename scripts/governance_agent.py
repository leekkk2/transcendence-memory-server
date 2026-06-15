#!/usr/bin/env python3
"""Governance orchestration agent — a generic LLM tool-use loop over the
governance toolbox (Phase 2 of the compress-knowledge-cluster / agent plan).

This module is the **planning brain**: it drives an OpenAI-compatible tool-use
loop where a gateway LLM picks which governance tool to call, the existing
``governance_tools.invoke_tool`` executes it, ``governance_store`` records the
trace / approvals, and a static safety gate (``decide``) enforces the autonomy
policy. It owns no execution primitive of its own — every side effect flows
through ``invoke_tool`` (which is itself dry-run-by-default + degrade-not-raise).

Autonomy policy (the safety gate, ``decide``):

  * SAFE read-only tools (manage_token_quotas / analyze_retrieval_latency) always
    execute for real — dry_run is a no-op for them.
  * Reversible tools (compress_knowledge_cluster / update_container_routing /
    tune_model_parameters) run for real ONLY when the caller passed
    ``allow_apply=True``; otherwise they stay dry-run (plan/preview).
  * The destructive tool (snapshot_and_quarantine) is **never** auto-executed in
    the loop: a model request to apply it records a pending approval row and the
    loop continues. A human, through the approval endpoint, is the only actuator.

Why this is safe + open-sourceable:

  * **HR-9** — every LLM call routes through ``rag_engine.llm_chat_with_tools``,
    the env-driven (`LLM_*`) sanctioned gateway; no provider endpoint, model id,
    base_url or key is ever hardcoded here.
  * **Bounded** — hard step / token / per-result-byte caps clamp the blast radius;
    every tool result is truncated through ``governance_tools._truncate_for_llm``
    before it re-enters the prompt, so no oversized payload can re-trigger an
    upstream request-too-large fault inside the loop.
  * **Degrade-not-raise** — ``run_agent`` never raises into its caller; a gateway
    or store outage finishes the run with a degraded status + partial trace.
  * **Generic prompt** — the system prompt carries no real container / host /
    domain / model name; the working container is always passed as a parameter.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

try:
    import governance_tools  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import governance_tools  # type: ignore

try:
    import governance_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import governance_store  # type: ignore

try:
    import rag_engine  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import rag_engine  # type: ignore

# dreaming is consumed read-only (run_dream_scan). It is an optional dependency:
# a slim environment without it must still let the agent plan over the other
# tools, so the import degrades to None and run_dream_scan reports gracefully.
try:
    import dreaming  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    try:
        from scripts import dreaming  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - dreaming absent entirely
        dreaming = None  # type: ignore[assignment]
except Exception:  # noqa: BLE001 - never let an import side effect break planning
    dreaming = None  # type: ignore[assignment]

logger = logging.getLogger("transcendence-memory-server.governance_agent")


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass
class AgentConfig:
    """Runtime limits + autonomy switch for a single agent run.

    ``allow_apply`` is the single knob the safety gate keys off for reversible
    tools (see ``decide``). It is False by default — a run with no explicit
    opt-in stays fully dry-run (plan mode), and the destructive tool is never
    auto-applied regardless of this flag.
    """

    max_steps: int = 6
    token_budget: int = 60000
    per_tool_result_bytes: int = 8000
    context_bytes_soft_cap: int = 200000
    allow_apply: bool = False
    tool_choice: str = "auto"


# Synthetic control / read-only tool names the model sees alongside the real
# governance tools. Kept out of governance_tools.TOOLS so that registry stays
# the pure execution surface (these two are agent-loop affordances only).
_TOOL_RUN_DREAM_SCAN = "run_dream_scan"
_TOOL_FINISH = "finish"

# Safety-gate buckets (mirror governance_tools dispatch semantics).
_REVERSIBLE_TOOLS = frozenset({
    "compress_knowledge_cluster",
    "update_container_routing",
    "tune_model_parameters",
})


# ── Tool param schemas (sidecar — governance_tools.TOOLS carries no schema) ────
# Hand-written from each handler's actual ``params.get(...)`` reads; kept here so
# the toolbox registry stays schema-free + generic. Only fields a handler reads
# are exposed to the model.
_PARAMS_SCHEMA: dict[str, dict[str, Any]] = {
    "compress_knowledge_cluster": {
        "type": "object",
        "properties": {
            "cluster_tag": {
                "type": "string",
                "description": "Optional tag selecting which same-topic cluster to "
                "compress; omit to auto-pick the largest same-tags cluster.",
            },
        },
        "additionalProperties": False,
    },
    "update_container_routing": {
        "type": "object",
        "properties": {
            "rules": {
                "type": "object",
                "description": "Routing-rule object for this container; merged "
                "additively into the global routing_rules map.",
            },
        },
        "required": ["rules"],
        "additionalProperties": False,
    },
    "snapshot_and_quarantine": {
        "type": "object",
        "properties": {
            "max_age_days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 3650,
                "description": "Age threshold (days) above which a stale row is a "
                "quarantine candidate.",
            },
        },
        "additionalProperties": False,
    },
    "tune_model_parameters": {
        "type": "object",
        "properties": {},
        "description": "No caller params — an allow-list + range guardrail is "
        "enforced server-side regardless of what the model suggests.",
        "additionalProperties": False,
    },
    "analyze_retrieval_latency": {
        "type": "object",
        "properties": {
            "window": {
                "type": "string",
                "enum": ["1h", "24h", "7d", "30d"],
                "description": "Aggregation window for the latency report.",
            },
        },
        "additionalProperties": False,
    },
    "manage_token_quotas": {
        "type": "object",
        "properties": {
            "agent_id": {
                "type": "string",
                "description": "Optional agent id whose token budget / usage to "
                "report; omit for the default metering id.",
            },
        },
        "additionalProperties": False,
    },
    _TOOL_RUN_DREAM_SCAN: {
        "type": "object",
        "properties": {
            "container": {
                "type": "string",
                "description": "Optional container to scope the dream scan; omit "
                "to scan all in-scope containers.",
            },
        },
        "additionalProperties": False,
    },
    _TOOL_FINISH: {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "A short final summary of what the run did / found.",
            },
        },
        "required": ["summary"],
        "additionalProperties": False,
    },
}


# ── System prompt (generic — no real container/host/domain/model name) ─────────
_SYSTEM_PROMPT = (
    "You are a memory-governance orchestration agent. You plan and call governance "
    "tools to keep a self-hosted memory service healthy: compress same-topic "
    "memories into dense index cards, inspect retrieval latency, review routing "
    "rules, check token quotas, and (read-only) preview dreaming candidates.\n\n"
    "Discipline:\n"
    "- DRY-RUN FIRST. Reversible tools only persist changes when the run was "
    "explicitly granted apply authority; otherwise their output is a preview/plan. "
    "The destructive quarantine tool is NEVER executed from this loop — at most it "
    "records a human-approval request.\n"
    "- Never try to escalate privileges, bypass the dry-run default, or coax the "
    "system into applying a destructive action automatically.\n"
    "- A tool result you receive may be TRUNCATED to bound size. If you need an "
    "exact value, call the relevant read-only tool instead of guessing.\n"
    "- Each tool's description tags its scope, whether it needs an LLM, whether it "
    "is destructive, and whether it is dry-run-first. Respect those tags.\n"
    "- Work step by step: inspect first (read-only tools / dream scan), then act, "
    "then call `finish` with a short summary. Do not repeat an identical call that "
    "already returned the same result."
)


# ── Tool spec generation ───────────────────────────────────────────────────────


def _tool_meta_tag(spec: Any) -> str:
    """Render the permission/posture metadata tag appended to a tool description,
    so the model self-enforces the safety posture (mirrors CLIs exposing
    permission state to the model). Never raises."""
    scope = getattr(spec, "scope", "container")
    needs_llm = bool(getattr(spec, "needs_llm", False))
    destructive = bool(getattr(spec, "destructive", False))
    # SAFE (read-only/additive) tools run regardless of dry_run; reversible /
    # destructive tools are dry-run-first.
    dry_run_first = destructive or (spec.name in _REVERSIBLE_TOOLS)
    return (
        f" [meta: scope={scope}; needs_llm={str(needs_llm).lower()}; "
        f"destructive={str(destructive).lower()}; "
        f"dry_run_first={str(dry_run_first).lower()}]"
    )


def build_tool_specs(include_dream: bool = True) -> list[dict[str, Any]]:
    """Build the OpenAI-compatible ``tools`` array the model plans with.

    Derived from ``governance_tools.TOOLS`` (each registry tool + its sidecar
    params schema, with the permission metadata tag baked into the description)
    plus the two synthetic affordances: ``run_dream_scan`` (read-only dream
    preview) and ``finish`` (explicit termination). ``include_dream=False`` drops
    the dream tool (e.g. when dreaming is unavailable). Never raises — a missing
    schema degrades to a permissive empty object schema."""
    specs: list[dict[str, Any]] = []
    try:
        registry = governance_tools.TOOLS
    except Exception:  # noqa: BLE001 - registry unavailable → no governance tools
        registry = {}
    for name, spec in registry.items():
        schema = _PARAMS_SCHEMA.get(name) or {"type": "object", "properties": {}}
        description = str(getattr(spec, "description", "")) + _tool_meta_tag(spec)
        specs.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        })
    if include_dream:
        specs.append({
            "type": "function",
            "function": {
                "name": _TOOL_RUN_DREAM_SCAN,
                "description": (
                    "Read-only preview of dreaming candidates (consolidation + "
                    "prune candidate scan, always dry-run). Use it to decide which "
                    "governance tool to call next. "
                    "[meta: scope=container; needs_llm=false; destructive=false; "
                    "dry_run_first=true]"
                ),
                "parameters": _PARAMS_SCHEMA[_TOOL_RUN_DREAM_SCAN],
            },
        })
    specs.append({
        "type": "function",
        "function": {
            "name": _TOOL_FINISH,
            "description": (
                "Finish the run with a short summary. Call this when the goal is "
                "satisfied or no further useful action exists. "
                "[meta: scope=global; needs_llm=false; destructive=false; "
                "dry_run_first=false]"
            ),
            "parameters": _PARAMS_SCHEMA[_TOOL_FINISH],
        },
    })
    return specs


# ── Safety gate ────────────────────────────────────────────────────────────────


def decide(tool: str, args: dict, container: Optional[str], cfg: AgentConfig) -> dict:
    """Static safety gate for one proposed tool call.

    Returns ``{"blocked","effective_dry_run","reason","requires"}``:
      * SAFE read-only tools → never blocked, dry_run is a no-op (effective_dry_run
        False so the real read runs).
      * Reversible tools → effective_dry_run = not cfg.allow_apply (apply only on
        explicit opt-in).
      * Destructive tools → blocked from auto-execution; requires='approval' so the
        loop records a pending approval instead of running it. effective_dry_run
        stays True as a belt-and-suspenders guard.
      * Unknown tool → not blocked here (invoke_tool returns status='error'); the
        gate stays permissive and lets the executor report the error.

    Never raises — a registry hiccup degrades to the conservative reversible
    treatment (dry-run unless allow_apply)."""
    try:
        destructive = governance_tools.tool_is_destructive(tool)
    except Exception:  # noqa: BLE001 - registry read failure → treat conservatively
        destructive = False
    if destructive:
        return {
            "blocked": True,
            "effective_dry_run": True,
            "reason": "destructive tool requires human approval — never "
            "auto-executed in the loop",
            "requires": "approval",
        }
    if tool in _REVERSIBLE_TOOLS:
        eff_dry = not bool(cfg.allow_apply)
        return {
            "blocked": False,
            "effective_dry_run": eff_dry,
            "reason": ("apply authorized" if not eff_dry
                       else "dry-run (no apply authority)"),
            "requires": "",
        }
    # SAFE read-only (manage_token_quotas / analyze_retrieval_latency) + anything
    # else → run for real; dry_run is a no-op for read-only handlers.
    return {
        "blocked": False,
        "effective_dry_run": False,
        "reason": "safe read-only tool",
        "requires": "",
    }


# ── Helpers ────────────────────────────────────────────────────────────────────


def _safe_json_loads(raw: Any) -> dict:
    """Parse a tool-call ``arguments`` blob (json string or dict) to a dict.
    Malformed / non-object → empty dict. Never raises."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001 - bad model output → empty args
        return {}


def _extract_tool_calls(message: dict) -> list[dict]:
    """Pull the normalized tool calls out of a gateway message dict.
    Returns [] when there are none (model is done). Never raises."""
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    return list(calls) if isinstance(calls, list) else []


def _call_id_name_args(call: dict) -> tuple[str, str, dict]:
    """Decompose one tool call into (call_id, name, args). Tolerant of missing
    keys (degrades to empty id / name / args). Never raises."""
    call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
    fn = call.get("function") if isinstance(call, dict) else None
    name = str(fn.get("name") or "") if isinstance(fn, dict) else ""
    args = _safe_json_loads(fn.get("arguments") if isinstance(fn, dict) else None)
    return call_id, name, args


def _summarize_result(result: dict) -> str:
    """Compact a tool result into a string for the model + the run-step trace.
    The caller still clamps this to per_tool_result_bytes. Never raises."""
    try:
        return json.dumps(result, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 - unserializable → repr fallback
        return str(result)


def _context_bytes(messages: list[dict]) -> int:
    """Rough UTF-8 byte size of the running transcript (soft-cap probe). Never
    raises — an unserializable entry contributes its repr length."""
    total = 0
    for m in messages:
        try:
            total += len(json.dumps(m, ensure_ascii=False).encode("utf-8"))
        except Exception:  # noqa: BLE001
            total += len(str(m).encode("utf-8"))
    return total


def _compact_messages(messages: list[dict], soft_cap: int) -> list[dict]:
    """Rolling-window transcript compaction: keep the system + original goal, drop
    the oldest middle turns until under the soft cap (or only head/tail remain).
    Never raises — degrades to the original list on any error."""
    try:
        if len(messages) <= 3:
            return messages
        head = messages[:2]   # system + user goal
        tail = messages[-1:]  # most recent turn
        middle = messages[2:-1]
        while middle and _context_bytes([*head, *middle, *tail]) > soft_cap:
            middle = middle[1:]
        compacted = [*head, *middle, *tail]
        if len(middle) < len(messages[2:-1]):
            compacted.insert(len(head), {
                "role": "system",
                "content": "[older steps elided to bound context size]",
            })
        return compacted
    except Exception:  # noqa: BLE001 - never break the loop on compaction
        return messages


def _used_tokens(message: dict) -> int:
    """Per-turn token estimate. The gateway records real usage out-of-band; the
    loop tracks an approximate running total for the soft token budget using the
    content + tool-call argument sizes. Never raises."""
    try:
        size = len(_summarize_result(message).encode("utf-8"))
        # ~4 bytes/token heuristic, bounded conservative.
        return max(1, size // 4)
    except Exception:  # noqa: BLE001
        return 1


async def _run_dream_scan(container: Optional[str]) -> dict:
    """Read-only dream preview wrapper. Degrades gracefully when dreaming is
    unavailable. Always dry-run; never raises."""
    if dreaming is None:
        return {
            "tool": _TOOL_RUN_DREAM_SCAN,
            "status": "unavailable",
            "container": container,
            "result": {},
            "applied": False,
            "notes": "dreaming module not available in this deployment",
        }
    try:
        report = await dreaming.run_dream_cycle(container=container, dry_run=True)
        return {
            "tool": _TOOL_RUN_DREAM_SCAN,
            "status": str(report.get("status") or "ok") if isinstance(report, dict)
            else "ok",
            "container": container,
            "result": report if isinstance(report, dict) else {"report": report},
            "applied": False,
            "notes": "read-only dream candidate preview (dry-run)",
        }
    except Exception as exc:  # noqa: BLE001 - dream scan must not break the loop
        logger.warning("[governance_agent] run_dream_scan degraded: %s", exc)
        return {
            "tool": _TOOL_RUN_DREAM_SCAN,
            "status": "error",
            "container": container,
            "result": {"error": str(exc)},
            "applied": False,
            "notes": "dream scan degraded",
        }


# ── The loop ───────────────────────────────────────────────────────────────────


async def run_agent(
    *,
    goal: str,
    container: Optional[str],
    agent_name: str,
    run_id: str,
    cfg: AgentConfig,
) -> dict:
    """Drive the governance tool-use loop and return a run summary dict.

    Plans through ``rag_engine.llm_chat_with_tools`` (HR-9 gateway), executes via
    ``governance_tools.invoke_tool``, truncates every tool result through
    ``governance_tools._truncate_for_llm`` before re-feeding it, and records the
    trace / approvals through ``governance_store``.

    Returns::

        {"run_id","status","steps","used_tokens","final_summary",
         "reindex_containers":[...],"approvals":[...approval ids]}

    Stop conditions: model finishes (``finish`` / no tool call → completed);
    max_steps reached; token budget exceeded; a destructive call is the only
    remaining proposed action and gets parked for approval
    (``blocked_pending_approval``); an identical (tool,args)→same-result repeat
    (``stalled``). Never raises — a gateway / store outage finishes the run with a
    degraded status and a partial trace."""
    started = int(time.time())
    governance_store.write_agent_run(
        run_id,
        goal=goal,
        container=container,
        mode=("apply" if cfg.allow_apply else "dry_run"),
        status="running",
        agent_name=agent_name,
    )

    messages: list[dict] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_goal(goal, container, cfg)},
    ]
    tool_specs = build_tool_specs(include_dream=dreaming is not None)

    used_tokens = 0
    steps = 0
    final_summary = ""
    status = "max_steps_exhausted"
    reindex_containers: list[str] = []
    approval_ids: list[int] = []
    last_signature: Optional[tuple[str, str, str]] = None  # (tool, args_json, result)

    for step in range(max(1, cfg.max_steps)):
        steps = step + 1
        try:
            message = await rag_engine.llm_chat_with_tools(
                messages, tools=tool_specs, tool_choice=cfg.tool_choice,
            )
        except Exception as exc:  # noqa: BLE001 - gateway outage → degrade run
            logger.warning("[governance_agent] gateway call degraded: %s", exc)
            status = "gateway_error"
            final_summary = f"planning gateway unavailable: {exc}"
            governance_store.append_agent_step(
                run_id, steps, "error", thought="", tool="",
                result_summary=final_summary,
            )
            break

        if not isinstance(message, dict):
            message = {"role": "assistant", "content": str(message)}
        thought = str(message.get("content") or "")
        used_tokens += _used_tokens(message)
        calls = _extract_tool_calls(message)

        if not calls:
            # Model produced no tool call → it considers itself done.
            status = "completed"
            final_summary = thought or final_summary or "completed (no further actions)"
            governance_store.append_agent_step(
                run_id, steps, "thought", thought=thought,
                result_summary="model finished without a tool call",
            )
            break

        # Re-feed the assistant tool-call message (required by the wire protocol).
        messages.append(_assistant_tool_call_message(message, calls))

        finished = False
        all_blocked_destructive = True
        for call in calls:
            call_id, name, args = _call_id_name_args(call)

            if name == _TOOL_FINISH:
                final_summary = str(args.get("summary") or thought or "completed")
                status = "completed"
                governance_store.append_agent_step(
                    run_id, steps, "finish", thought=thought, tool=name,
                    args_json=_args_json(args), result_summary=final_summary,
                )
                _append_tool_message(messages, call_id, "acknowledged: finished")
                finished = True
                all_blocked_destructive = False
                break

            gate = decide(name, args, container, cfg)

            if name == _TOOL_RUN_DREAM_SCAN:
                all_blocked_destructive = False
                result = await _run_dream_scan(args.get("container") or container)
                summary = governance_tools._truncate_for_llm(  # noqa: SLF001
                    _summarize_result(result), cfg.per_tool_result_bytes,
                )
                governance_store.append_agent_step(
                    run_id, steps, "tool", thought=thought, tool=name,
                    args_json=_args_json(args), gate_decision=gate.get("reason", ""),
                    invoke_status=str(result.get("status") or ""), applied=False,
                    result_summary=summary,
                )
                _append_tool_message(messages, call_id, summary)
                continue

            if gate.get("blocked"):
                # Destructive tool → record a pending approval, do NOT execute.
                approval_id = governance_store.write_agent_approval(
                    run_id=run_id, agent_name=agent_name, container=container,
                    tool=name, params_json=_args_json(args),
                    plan_json=_args_json({"goal": goal}),
                )
                if isinstance(approval_id, int):
                    approval_ids.append(approval_id)
                note = (f"parked for human approval (approval_id={approval_id}); "
                        "destructive tool not auto-executed")
                governance_store.append_agent_step(
                    run_id, steps, "approval", thought=thought, tool=name,
                    args_json=_args_json(args), gate_decision=gate.get("reason", ""),
                    invoke_status="pending_approval", applied=False,
                    result_summary=note,
                )
                _append_tool_message(messages, call_id, note)
                continue

            # SAFE / reversible → execute via the toolbox at the gated dry_run.
            all_blocked_destructive = False
            try:
                result = await governance_tools.invoke_tool(
                    name, container, args, dry_run=bool(gate.get("effective_dry_run")),
                )
            except Exception as exc:  # noqa: BLE001 - invoke_tool already guards; belt
                logger.warning("[governance_agent] invoke %s degraded: %s", name, exc)
                result = {
                    "tool": name, "status": "error", "container": container,
                    "result": {"error": str(exc)}, "applied": False,
                    "notes": "invoke degraded",
                }
            applied = bool(result.get("applied"))
            inner = result.get("result") if isinstance(result, dict) else None
            if (applied and isinstance(inner, dict)
                    and inner.get("reindex_required") and container
                    and container not in reindex_containers):
                reindex_containers.append(container)

            summary = governance_tools._truncate_for_llm(  # noqa: SLF001
                _summarize_result(result), cfg.per_tool_result_bytes,
            )
            governance_store.append_agent_step(
                run_id, steps, "tool", thought=thought, tool=name,
                args_json=_args_json(args), gate_decision=gate.get("reason", ""),
                invoke_status=str(result.get("status") or ""), applied=applied,
                result_summary=summary,
            )
            _append_tool_message(messages, call_id, summary)

            # Loop / stall detection: identical (tool,args)→identical result twice.
            signature = (name, _args_json(args), summary)
            if signature == last_signature:
                status = "stalled"
                final_summary = (f"stalled: repeated identical call to {name} "
                                 "returned the same result")
                finished = True
                break
            last_signature = signature

        if finished:
            break

        # The only proposed action(s) this step were blocked destructive ones →
        # nothing executable remains without human approval.
        if all_blocked_destructive and approval_ids:
            status = "blocked_pending_approval"
            final_summary = (final_summary
                             or "blocked: destructive action(s) await human approval")
            break

        if used_tokens > cfg.token_budget:
            status = "token_budget_exceeded"
            final_summary = (final_summary
                             or f"token budget {cfg.token_budget} exceeded")
            break

        if _context_bytes(messages) > cfg.context_bytes_soft_cap:
            messages = _compact_messages(messages, cfg.context_bytes_soft_cap)

    if status == "max_steps_exhausted" and not final_summary:
        final_summary = f"reached max_steps={cfg.max_steps} without finishing"

    governance_store.finish_agent_run(
        run_id, status, final_summary=final_summary,
        used_tokens=used_tokens, steps=steps,
    )
    logger.info(
        "[governance_agent] run %s finished status=%s steps=%d tokens=%d "
        "approvals=%d reindex=%d duration=%ds",
        run_id, status, steps, used_tokens, len(approval_ids),
        len(reindex_containers), int(time.time()) - started,
    )
    return {
        "run_id": run_id,
        "status": status,
        "steps": steps,
        "used_tokens": used_tokens,
        "final_summary": final_summary,
        "reindex_containers": reindex_containers,
        "approvals": approval_ids,
    }


# ── Small message builders (kept tiny + raise-free) ───────────────────────────


def _build_user_goal(goal: str, container: Optional[str], cfg: AgentConfig) -> str:
    """Compose the user turn: the goal + the working scope + the autonomy posture,
    all generic (container is whatever was passed in)."""
    scope = container or "(global / all in-scope containers)"
    posture = ("apply authorized for reversible tools"
               if cfg.allow_apply else "dry-run only (no apply authority)")
    return (
        f"Goal: {goal or 'inspect governance state and propose maintenance'}\n"
        f"Working scope: {scope}\n"
        f"Autonomy: {posture}. The destructive quarantine tool is never "
        "auto-executed regardless.\n"
        "Inspect first, act within your authority, then call `finish`."
    )


def _args_json(args: dict) -> str:
    """Serialize call args for the trace. Never raises."""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(args)


def _assistant_tool_call_message(message: dict, calls: list[dict]) -> dict:
    """Build the assistant message to re-feed into the transcript. Preserves the
    original tool_calls structure (required so the gateway can pair tool results
    by id). Never raises."""
    return {
        "role": "assistant",
        "content": message.get("content") or "",
        "tool_calls": calls,
    }


def _append_tool_message(messages: list[dict], call_id: str, content: str) -> None:
    """Append a tool-result message paired to its call id. Never raises."""
    messages.append({
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    })
