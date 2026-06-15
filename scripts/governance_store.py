#!/usr/bin/env python3
"""Independent governance-output store (blueprint P6, dreaming + toolbox).

This store is the **structural guarantee of RAG immunity**. Dreaming-cycle dream
reports (and any future governance artifact) are persisted HERE — a dedicated
``governance_reports`` SQLite table — and are *physically separate* from the
per-container user-retrieval corpora (the LanceDB tables under
``WORKSPACE/tasks/rag/containers/<container>/lancedb``).

Why this is enough (and why the main query path is NOT touched):

  * The main ``/search`` / ``/query`` retrieval reads ONLY a user container's
    LanceDB table. It never reads this governance DB. So governance output can
    never surface in a user's RAG results — not because of any
    ``exclude_from_rag`` *filter* in the query path (there is none, and none is
    added), but because governance data **structurally lives in a different
    store that the query path does not consult**.
  * Every row still carries ``system_type='governance'`` +
    ``exclude_from_rag=True`` metadata so the immunity intent is self-describing
    and auditable, and so any future cross-store tooling can honor it.

Invariants (mirror redis_client.py P0 / config_store.py P1):

  * **Import-safe** — importing opens no connection, touches no network.
  * **Graceful** — a missing/locked DB degrades every write to False and every
    read to None/[]; nothing here ever raises into a caller (least of all the
    request path). The dreaming engine treats a False write as "snapshot lost
    this cycle" and carries on.
  * **Behavior-preserving** — this module has zero readers in the RAG path;
    nothing it does can change ``/search`` / ``/query`` output.

R8: pure generic code — no private endpoint / hostname / credential / private
container name.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("transcendence-memory-server.governance")

# Immutable metadata stamped on every governance row — the self-describing
# RAG-immunity marker (see module docstring).
SYSTEM_TYPE = "governance"
_IMMUNITY_META = {"system_type": SYSTEM_TYPE, "exclude_from_rag": True}


def _governance_db_path() -> Path:
    """Resolve the governance DB path — the SAME queue.db the job queue / config
    store / token meter already use (single WAL / crash / purge domain), per the
    repo's persistence convention. WORKSPACE drives it so tests isolate cleanly.
    The governance_reports table is namespaced WITHIN that file; it does NOT mix
    with any user-container LanceDB corpus (those live under tasks/rag/containers).
    """
    ws = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
    return ws / "tasks" / "rag" / "queue.db"


def _connect() -> Optional[sqlite3.Connection]:
    """Open a short-lived connection with the schema ensured, or None on failure.

    Mirrors the JobQueue/ConfigKVStore sqlite façade (per-call connection, WAL,
    busy timeout). Returns None — never raises — when the DB can't be opened so
    callers degrade silently.
    """
    try:
        path = _governance_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    INTEGER NOT NULL,
                system_type   TEXT    NOT NULL,
                container     TEXT,
                report_json   TEXT    NOT NULL
            )
            """
        )
        # Governance-agent orchestration trio — run head / per-step trace /
        # destructive-action approval queue. Same queue.db, same WAL/purge
        # domain, same system_type='governance' + exclude_from_rag immunity
        # stamping as governance_reports (the query path never reads these).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id        TEXT    PRIMARY KEY,
                created_at    INTEGER NOT NULL,
                finished_at   INTEGER,
                agent_name    TEXT    NOT NULL DEFAULT '',
                goal          TEXT    NOT NULL DEFAULT '',
                container     TEXT,
                mode          TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT '',
                steps         INTEGER NOT NULL DEFAULT 0,
                used_tokens   INTEGER NOT NULL DEFAULT 0,
                final_summary TEXT    NOT NULL DEFAULT '',
                job_id        INTEGER,
                system_type   TEXT    NOT NULL DEFAULT 'governance',
                exclude_from_rag INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_run_steps (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id         TEXT    NOT NULL,
                step           INTEGER NOT NULL,
                ts             INTEGER NOT NULL,
                kind           TEXT    NOT NULL DEFAULT '',
                thought        TEXT    NOT NULL DEFAULT '',
                tool           TEXT    NOT NULL DEFAULT '',
                args_json      TEXT    NOT NULL DEFAULT '',
                gate_decision  TEXT    NOT NULL DEFAULT '',
                invoke_status  TEXT    NOT NULL DEFAULT '',
                applied        INTEGER NOT NULL DEFAULT 0,
                result_summary TEXT    NOT NULL DEFAULT '',
                system_type    TEXT    NOT NULL DEFAULT 'governance',
                exclude_from_rag INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_approvals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at    INTEGER NOT NULL,
                run_id        TEXT    NOT NULL DEFAULT '',
                agent_name    TEXT    NOT NULL DEFAULT '',
                container     TEXT,
                tool          TEXT    NOT NULL DEFAULT '',
                params_json   TEXT    NOT NULL DEFAULT '',
                plan_json     TEXT    NOT NULL DEFAULT '',
                status        TEXT    NOT NULL DEFAULT 'pending',
                decided_at    INTEGER,
                decided_by    TEXT    NOT NULL DEFAULT '',
                system_type   TEXT    NOT NULL DEFAULT 'governance',
                exclude_from_rag INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        return conn
    except Exception as exc:  # noqa: BLE001 - DB unavailable → degrade, never raise
        logger.warning("[governance] DB open failed, store disabled: %s", exc)
        return None


def write_dream_report(report: dict) -> bool:
    """Persist a dream report into the isolated governance store. Returns success.

    The stored row is force-stamped with the RAG-immunity metadata
    (``system_type='governance'`` + ``exclude_from_rag=True``) regardless of what
    the caller passed — the marker is non-negotiable. A None/locked DB degrades
    to False (the dreaming cycle logs and continues); never raises.
    """
    if not isinstance(report, dict):
        return False
    enriched = {**report, **_IMMUNITY_META}
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "INSERT INTO governance_reports "
                "(created_at, system_type, container, report_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    int(time.time()),
                    SYSTEM_TYPE,
                    enriched.get("container_scope"),
                    json.dumps(enriched, ensure_ascii=False),
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] write_dream_report failed: %s", exc)
        return False


def _row_to_report(raw_json: str) -> Optional[dict]:
    try:
        obj = json.loads(raw_json)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - a corrupt row must not break a list read
        return None


def get_last_report() -> Optional[dict]:
    """Return the most recent dream report, or None (no rows / DB down). Graceful."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT report_json FROM governance_reports "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return None if row is None else _row_to_report(row["report_json"])
    except Exception as exc:  # noqa: BLE001 - read failure → degrade to None
        logger.warning("[governance] get_last_report failed: %s", exc)
        return None


def list_reports(limit: int = 20) -> list[dict]:
    """Return up to `limit` recent dream reports (newest first). [] on any failure."""
    safe_limit = max(1, min(int(limit), 500)) if isinstance(limit, int) else 20
    conn = _connect()
    if conn is None:
        return []
    try:
        with closing(conn):
            rows = conn.execute(
                "SELECT report_json FROM governance_reports "
                "ORDER BY id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            rep = _row_to_report(r["report_json"])
            if rep is not None:
                out.append(rep)
        return out
    except Exception as exc:  # noqa: BLE001 - read failure → degrade to empty
        logger.warning("[governance] list_reports failed: %s", exc)
        return []


# ── Governance-agent orchestration: run / step / approval helpers ────────────
# All three tables share the immunity stamping and the graceful façade above:
# every write degrades to False/None on a missing/locked DB and never raises
# into the runner / endpoint / loop. Reads degrade to None/[].


def _approval_ttl_days() -> int:
    """Read the shared approval TTL (config:tools:approval_ttl_days). Lazy import
    keeps this module import-safe; any failure degrades to the registry default
    (30 days) — never raises."""
    try:
        try:
            import config_store  # type: ignore
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts import config_store  # type: ignore
        val = int(config_store.get_cached("config:tools:approval_ttl_days", 30))
        return val if val > 0 else 30
    except Exception:  # noqa: BLE001 - config unavailable → conservative default
        return 30


def write_agent_run(
    run_id: str,
    *,
    goal: str,
    container: Optional[str],
    mode: str,
    status: str,
    agent_name: str = "",
    job_id: Optional[int] = None,
) -> bool:
    """Insert (or replace) an agent-run head row. Returns success; degrades to
    False on a None/locked DB (the caller logs and carries on); never raises."""
    if not run_id:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "INSERT OR REPLACE INTO agent_runs "
                "(run_id, created_at, agent_name, goal, container, mode, status, "
                " steps, used_tokens, final_summary, job_id, system_type, exclude_from_rag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?, 1)",
                (
                    str(run_id),
                    int(time.time()),
                    str(agent_name or ""),
                    str(goal or ""),
                    container,
                    str(mode or ""),
                    str(status or ""),
                    int(job_id) if job_id is not None else None,
                    SYSTEM_TYPE,
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] write_agent_run failed: %s", exc)
        return False


def append_agent_step(
    run_id: str,
    step: int,
    kind: str,
    *,
    thought: str = "",
    tool: str = "",
    args_json: str = "",
    gate_decision: str = "",
    invoke_status: str = "",
    applied: bool = False,
    result_summary: str = "",
) -> bool:
    """Append one trace row for an agent run. Returns success; degrades to False
    on a None/locked DB; never raises (a lost step must not abort the loop)."""
    if not run_id:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "INSERT INTO agent_run_steps "
                "(run_id, step, ts, kind, thought, tool, args_json, gate_decision, "
                " invoke_status, applied, result_summary, system_type, exclude_from_rag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    str(run_id),
                    int(step),
                    int(time.time()),
                    str(kind or ""),
                    str(thought or ""),
                    str(tool or ""),
                    str(args_json or ""),
                    str(gate_decision or ""),
                    str(invoke_status or ""),
                    1 if applied else 0,
                    str(result_summary or ""),
                    SYSTEM_TYPE,
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] append_agent_step failed: %s", exc)
        return False


def finish_agent_run(
    run_id: str,
    status: str,
    *,
    final_summary: str = "",
    used_tokens: int = 0,
    steps: int = 0,
) -> bool:
    """Mark an agent run finished (terminal status + summary + tallies). Returns
    success; degrades to False on a None/locked DB; never raises."""
    if not run_id:
        return False
    conn = _connect()
    if conn is None:
        return False
    try:
        with closing(conn):
            conn.execute(
                "UPDATE agent_runs SET finished_at = ?, status = ?, "
                "final_summary = ?, used_tokens = ?, steps = ? WHERE run_id = ?",
                (
                    int(time.time()),
                    str(status or ""),
                    str(final_summary or ""),
                    int(used_tokens or 0),
                    int(steps or 0),
                    str(run_id),
                ),
            )
        return True
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] finish_agent_run failed: %s", exc)
        return False


def get_agent_run(run_id: str) -> Optional[dict]:
    """Return one agent-run head row as a dict, or None (no row / DB down)."""
    if not run_id:
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE run_id = ? LIMIT 1",
                (str(run_id),),
            ).fetchone()
        return None if row is None else dict(row)
    except Exception as exc:  # noqa: BLE001 - read failure → degrade to None
        logger.warning("[governance] get_agent_run failed: %s", exc)
        return None


def list_agent_runs(limit: int = 50) -> list[dict]:
    """Return up to `limit` recent agent runs (newest first). [] on any failure."""
    safe_limit = max(1, min(int(limit), 500)) if isinstance(limit, int) else 50
    conn = _connect()
    if conn is None:
        return []
    try:
        with closing(conn):
            rows = conn.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001 - read failure → degrade to empty
        logger.warning("[governance] list_agent_runs failed: %s", exc)
        return []


def write_agent_approval(
    *,
    run_id: str,
    agent_name: str,
    container: Optional[str],
    tool: str,
    params_json: str,
    plan_json: str = "",
) -> Optional[int]:
    """Record a pending destructive-action approval. Returns the new approval id,
    or None on a None/locked DB / write failure; never raises."""
    conn = _connect()
    if conn is None:
        return None
    try:
        with closing(conn):
            cur = conn.execute(
                "INSERT INTO agent_approvals "
                "(created_at, run_id, agent_name, container, tool, params_json, "
                " plan_json, status, decided_by, system_type, exclude_from_rag) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', ?, 1)",
                (
                    int(time.time()),
                    str(run_id or ""),
                    str(agent_name or ""),
                    container,
                    str(tool or ""),
                    str(params_json or ""),
                    str(plan_json or ""),
                    SYSTEM_TYPE,
                ),
            )
            return int(cur.lastrowid)
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] write_agent_approval failed: %s", exc)
        return None


def list_agent_approvals(status: str = "pending", limit: int = 50) -> list[dict]:
    """Return up to `limit` approvals filtered by `status` (newest first). Pending
    rows older than config:tools:approval_ttl_days are dropped as expired. [] on
    any failure."""
    safe_limit = max(1, min(int(limit), 500)) if isinstance(limit, int) else 50
    conn = _connect()
    if conn is None:
        return []
    try:
        cutoff = int(time.time()) - _approval_ttl_days() * 86400
        with closing(conn):
            rows = conn.execute(
                "SELECT * FROM agent_approvals WHERE status = ? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (str(status or "pending"), safe_limit),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            rec = dict(r)
            # Expired pendings are functionally void (re-approval required); hide.
            if str(status) == "pending" and int(rec.get("created_at", 0)) < cutoff:
                continue
            out.append(rec)
        return out
    except Exception as exc:  # noqa: BLE001 - read failure → degrade to empty
        logger.warning("[governance] list_agent_approvals failed: %s", exc)
        return []


def decide_agent_approval(
    approval_id: int, status: str, decided_by: str = ""
) -> Optional[dict]:
    """Transition a *pending* approval to approved/rejected and return its row
    (incl. tool/container/params_json) so the caller can execute. Returns None if
    the approval doesn't exist, isn't pending, has expired (TTL), or the DB is
    down; never raises. Only the pending→decided transition is honored."""
    try:
        approval_id = int(approval_id)
    except Exception:  # noqa: BLE001 - bad id → no-op
        return None
    conn = _connect()
    if conn is None:
        return None
    try:
        cutoff = int(time.time()) - _approval_ttl_days() * 86400
        with closing(conn):
            row = conn.execute(
                "SELECT * FROM agent_approvals WHERE id = ? LIMIT 1",
                (approval_id,),
            ).fetchone()
            if row is None:
                return None
            rec = dict(row)
            if str(rec.get("status")) != "pending":
                return None
            if int(rec.get("created_at", 0)) < cutoff:  # expired pending = void
                return None
            decided_at = int(time.time())
            cur = conn.execute(
                "UPDATE agent_approvals SET status = ?, decided_at = ?, "
                "decided_by = ? WHERE id = ? AND status = 'pending'",
                (str(status or ""), decided_at, str(decided_by or ""), approval_id),
            )
            # Exactly-once across concurrent approves: only the txn that actually
            # flips pending→decided (rowcount == 1) owns the executable row; a
            # racing approve sees rowcount == 0 (someone won first) → None, so the
            # caller's "already decided → 404" path fires and the destructive tool
            # never runs twice.
            if cur.rowcount != 1:
                return None
            rec["status"] = str(status or "")
            rec["decided_at"] = decided_at
            rec["decided_by"] = str(decided_by or "")
        return rec
    except Exception as exc:  # noqa: BLE001 - write failure → degrade, never raise
        logger.warning("[governance] decide_agent_approval failed: %s", exc)
        return None
