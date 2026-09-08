"""Output formatters — rich (default), JSON, and quiet."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from rich.console import Console
from rich.table import Table
from .redaction import redact_text


@dataclass
class OutputMode:
    """Resolved output mode for a single command invocation."""

    json: bool = False
    quiet: bool = False
    no_color: bool = False

    @property
    def is_rich(self) -> bool:
        return not (self.json or self.quiet)


def build_console(mode: OutputMode, *, file=None) -> Console:
    """Construct a ``rich.Console`` that respects ``--no-color`` / ``--quiet``."""

    return Console(
        no_color=mode.no_color,
        quiet=mode.quiet,
        file=file or sys.stdout,
        highlight=False,
        soft_wrap=True,
    )


def emit_json(payload: Any) -> None:
    """Dump ``payload`` to stdout as compact-ish JSON (jq-friendly)."""

    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error(message: str, *, mode: OutputMode | None = None) -> None:
    """Write an error message to stderr (always — even in --quiet)."""

    message=redact_text(message)
    if mode and mode.json:
        sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write(f"error: {message}\n")
    sys.stderr.flush()


def render_search_hits(hits: Sequence[dict[str, Any]], *, console: Console, query: str, container: str, topk: int, full: bool = False) -> None:
    """Render search hits as a rich table."""

    title = f"Search · container: {container} · query: {query} · topk: {topk}"
    table = Table(title=title, show_lines=False)
    table.add_column("Rank", justify="right", style="cyan", no_wrap=True)
    table.add_column("Vector distance ↓", justify="right", style="magenta")
    table.add_column("Rerank relevance ↑", justify="right")
    table.add_column("Snippet", overflow="fold")
    for idx, hit in enumerate(hits, start=1):
        score = hit.get("vector_distance")
        if score is None: score = hit.get("vectorScore")
        if score is None: score = hit.get("score")
        rerank = hit.get("rerank_score")
        if rerank is None: rerank = hit.get("rerankScore")
        rerank_str = f"{rerank:.3f}" if isinstance(rerank,(int,float)) and math.isfinite(rerank) else "—"
        score_str = f"{score:.3f}" if isinstance(score, (int, float)) and math.isfinite(score) else "—"
        snippet = (hit.get("text") or hit.get("title") or "").strip().replace("\n", " ")
        if not full and len(snippet) > 240:
            snippet = snippet[:237] + "…"
        table.add_row(str(idx), score_str, rerank_str, snippet or "(empty)")
    console.print(table)


def render_containers_table(rows: Iterable[dict[str, Any]], *, console: Console) -> None:
    """Render the ``/containers`` table for human consumption."""

    table = Table(show_header=True, header_style="bold")
    table.add_column("container")
    table.add_column("memory_count", justify="right")
    table.add_column("last_modified")
    table.add_column("index_state")
    for row in rows:
        table.add_row(
            str(row.get("name", "")),
            str(row.get("objects", 0)),
            str(row.get("last_modified") or "—"),
            str(row.get("index_state") or "unknown"),
        )
    console.print(table)


def render_status(payload: dict[str, Any], *, console: Console, endpoint: str, container: str | None) -> None:
    """Render the response of ``GET /health`` in a friendly block."""

    console.print(f"[bold]endpoint[/bold] : {endpoint}")
    if container:
        console.print(f"[bold]container[/bold]: {container}")
    console.print(f"[bold]status[/bold]   : {payload.get('status', '?')}")
    console.print(f"[bold]service[/bold]  : {payload.get('service', '?')}")
    console.print(
        f"[bold]flavor[/bold]   : {payload.get('build_flavor', '?')} "
        f"(multimodal={payload.get('multimodal_capable', False)})"
    )
    warnings = payload.get("warnings") or []
    if warnings:
        console.print("[yellow]warnings[/yellow]: " + "; ".join(warnings))
