#!/usr/bin/env python3
"""CLI subprocess entry point for one governance-agent run (Phase 3 of the
compress-knowledge-cluster / agent orchestration plan).

The background job worker resolves an ``op='run-agent'`` job to this script
(see ``job_worker.default_command_resolver``). It is a thin, fail-safe shell
around ``governance_agent.run_agent``:

  * builds an ``AgentConfig`` from ``config:agent:*`` / ``TM_AGENT_*`` (env wins),
  * enforces its own wall-clock ceiling via ``asyncio.wait_for`` (well below the
    worker's global job timeout) so an overrunning plan produces a *partial*
    report and exits non-zero **without a traceback** rather than being killed,
  * best-effort re-enqueues an ``embed`` job for every container the run flagged
    for re-index (the runner cannot import the server, so it talks to the job
    queue directly and degrades silently if the queue is unavailable).

Run accounting (``agent_runs`` head, per-step trace, pending approvals) is all
written inside ``run_agent`` via ``governance_store`` — this script only mints
the config + scope and prints a JSON summary line on stdout (the worker captures
the first 512 bytes as the job note).

HR-9: every LLM call inside the loop routes through ``rag_engine`` (the env-driven
``LLM_*`` gateway); nothing here hardcodes a model / base_url / api key.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import governance_agent  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import governance_agent  # type: ignore

try:
    import governance_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import governance_store  # type: ignore

# config_store is import-safe (no eager connect). It is an optional dependency:
# a slim environment without it falls back to env / hardcoded defaults so the
# runner still works.
try:
    import config_store  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - package import path
    try:
        from scripts import config_store  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - config store absent entirely
        config_store = None  # type: ignore[assignment]
except Exception:  # noqa: BLE001 - never let an import side effect break a run
    config_store = None  # type: ignore[assignment]

_logger = logging.getLogger("transcendence-memory-server.governance_agent_runner")
if not _logger.handlers and not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _config_int(key: str, env_name: str, default: int) -> int:
    """Resolve an int knob: ``TM_*`` env (highest priority) → ``config:agent:*``
    (config_store) → hardcoded default. Any parse / read failure degrades to the
    next source; never raises."""
    env_raw = os.environ.get(env_name)
    if env_raw is not None and env_raw != "":
        try:
            return int(env_raw)
        except (TypeError, ValueError):
            pass
    if config_store is not None:
        try:
            return int(config_store.get_cached(key, default))
        except Exception:  # noqa: BLE001 - config unavailable → default
            pass
    return default


def _load_params(params_file: Optional[str]) -> dict[str, Any]:
    """Read the run params JSON file written by the endpoint. A missing / malformed
    file degrades to an empty dict (the run just has no extra params); never raises."""
    if not params_file:
        return {}
    try:
        raw = Path(params_file).read_text(encoding="utf-8")
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception as exc:  # noqa: BLE001 - bad params file → empty params
        _logger.warning("params file unreadable, using empty params: %s", exc)
        return {}


def _build_config(allow_apply: bool) -> tuple[governance_agent.AgentConfig, int]:
    """Build the ``AgentConfig`` + resolve the wall-clock ceiling (seconds).

    ``max_steps`` comes from ``config:agent:max_steps`` / ``TM_AGENT_MAX_STEPS``;
    the timeout from ``config:agent:run_timeout_sec`` / ``TM_AGENT_RUN_TIMEOUT_SEC``.
    Other ``AgentConfig`` fields keep their dataclass defaults (token / byte caps).
    """
    max_steps = max(1, _config_int("config:agent:max_steps", "TM_AGENT_MAX_STEPS", 6))
    run_timeout_sec = max(
        1, _config_int("config:agent:run_timeout_sec", "TM_AGENT_RUN_TIMEOUT_SEC", 300)
    )
    cfg = governance_agent.AgentConfig(
        max_steps=max_steps,
        allow_apply=bool(allow_apply),
    )
    return cfg, run_timeout_sec


def _queue_db_path() -> Path:
    """Same ``WORKSPACE/tasks/rag/queue.db`` the server / worker use. WORKSPACE
    drives it so a test workspace stays isolated."""
    ws = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
    return ws / "tasks" / "rag" / "queue.db"


def _reindex_best_effort(containers: list[str]) -> list[str]:
    """Best-effort: enqueue an ``embed`` job for each container the run flagged for
    re-index. The runner cannot import the server, so it talks to the job queue
    directly. A missing queue module / DB / enqueue failure degrades silently
    (re-embed is an ancillary action — the run already succeeded); never raises.
    Returns the containers actually enqueued."""
    if not containers:
        return []
    try:
        try:
            from job_queue import JobQueue  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.job_queue import JobQueue  # type: ignore
    except Exception as exc:  # noqa: BLE001 - queue module unavailable → skip
        _logger.warning("reindex skipped, job_queue unavailable: %s", exc)
        return []
    enqueued: list[str] = []
    try:
        queue = JobQueue(_queue_db_path())
    except Exception as exc:  # noqa: BLE001 - queue open failed → skip all
        _logger.warning("reindex skipped, queue open failed: %s", exc)
        return []
    for container in containers:
        if not container:
            continue
        try:
            queue.enqueue(
                op="embed",
                container=container,
                payload={},
                label="governance-reindex",
                coalesce=True,
            )
            enqueued.append(container)
        except Exception as exc:  # noqa: BLE001 - one container failing must not abort
            _logger.warning(
                "reindex enqueue degraded for container=%s: %s", container, exc
            )
            continue
    return enqueued


async def _run(
    *,
    goal: str,
    container: Optional[str],
    agent_name: str,
    run_id: str,
    cfg: governance_agent.AgentConfig,
    run_timeout_sec: int,
) -> tuple[dict[str, Any], bool]:
    """Drive ``run_agent`` under a self-imposed wall-clock ceiling. Returns
    ``(report, timed_out)``. On timeout the loop is cancelled and we synthesize a
    partial report + flip the run head to ``timeout`` so the trace is consistent.
    Never raises into ``main``."""
    try:
        report = await asyncio.wait_for(
            governance_agent.run_agent(
                goal=goal,
                container=container,
                agent_name=agent_name,
                run_id=run_id,
                cfg=cfg,
            ),
            timeout=run_timeout_sec,
        )
        return report, False
    except asyncio.TimeoutError:
        # Wall-clock ceiling hit: record a partial report so the run head is not
        # left dangling at 'running'. finish_agent_run degrades to False if the
        # store is down — that is fine, we still return a structured summary.
        _logger.warning(
            "run %s exceeded wall-clock ceiling %ss; recording partial report",
            run_id, run_timeout_sec,
        )
        summary = f"run timed out after {run_timeout_sec}s wall-clock ceiling"
        try:
            governance_store.finish_agent_run(
                run_id, "timeout", final_summary=summary, used_tokens=0, steps=0,
            )
        except Exception as exc:  # noqa: BLE001 - store outage → degrade
            _logger.warning("finish_agent_run on timeout degraded: %s", exc)
        return (
            {
                "run_id": run_id,
                "status": "timeout",
                "steps": 0,
                "used_tokens": 0,
                "final_summary": summary,
                "reindex_containers": [],
                "approvals": [],
            },
            True,
        )
    except Exception as exc:  # noqa: BLE001 - run_agent already guards; belt + braces
        _logger.warning("run %s degraded: %s", run_id, exc)
        summary = f"run degraded: {exc}"
        try:
            governance_store.finish_agent_run(
                run_id, "error", final_summary=summary, used_tokens=0, steps=0,
            )
        except Exception as exc2:  # noqa: BLE001
            _logger.warning("finish_agent_run on error degraded: %s", exc2)
        return (
            {
                "run_id": run_id,
                "status": "error",
                "steps": 0,
                "used_tokens": 0,
                "final_summary": summary,
                "reindex_containers": [],
                "approvals": [],
            },
            False,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one governance-agent loop.")
    parser.add_argument("--container", default="")
    parser.add_argument("--agent-name", default="dream-orchestrator")
    parser.add_argument("--goal", default="")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--params-file", default="")
    parser.add_argument("--allow-apply", action="store_true")
    args = parser.parse_args()

    started = time.time()
    params = _load_params(args.params_file or None)
    # The goal may also ride in the params file (endpoint convenience); the
    # explicit --goal flag wins when both are present.
    goal = args.goal or str(params.get("goal") or "")
    container = args.container or None
    cfg, run_timeout_sec = _build_config(args.allow_apply)

    _logger.info(
        "governance-agent run start run_id=%s agent=%s container=%s "
        "max_steps=%d timeout=%ds allow_apply=%s",
        args.run_id, args.agent_name, container or "(global)",
        cfg.max_steps, run_timeout_sec, bool(args.allow_apply),
    )

    report, timed_out = asyncio.run(
        _run(
            goal=goal,
            container=container,
            agent_name=args.agent_name,
            run_id=args.run_id,
            cfg=cfg,
            run_timeout_sec=run_timeout_sec,
        )
    )

    reindex_containers = report.get("reindex_containers") or []
    reindexed = _reindex_best_effort(
        [c for c in reindex_containers if isinstance(c, str)]
    )

    out = {
        "code": 0 if not timed_out else 1,
        "run_id": args.run_id,
        "agent_name": args.agent_name,
        "container": container,
        "status": report.get("status"),
        "steps": report.get("steps"),
        "used_tokens": report.get("used_tokens"),
        "approvals": report.get("approvals") or [],
        "reindex_enqueued": reindexed,
        "duration_sec": round(time.time() - started, 2),
    }
    print(json.dumps(out, ensure_ascii=False))
    # Non-zero on timeout so the worker records it as a (retriable) failed attempt;
    # a clean run returns 0 even if the loop ended in a degraded/blocked status
    # (those are legitimate terminal outcomes, not subprocess failures).
    return 1 if timed_out else 0


if __name__ == "__main__":
    sys.exit(main())
