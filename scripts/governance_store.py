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
