"""Output formatters — rich (default), JSON, and quiet."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from rich.console import Console
from rich.table import Table


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

    if mode and mode.json:
        sys.stderr.write(json.dumps({"error": message}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write(f"error: {message}\n")
    sys.stderr.flush()


def render_search_hits(hits: Sequence[dict[str, Any]], *, console: Console, query: str, container: str, topk: int) -> None:
    """Render search hits as a rich table."""

    title = f"Search · container: {container} · query: {query} · topk: {topk}"
    table = Table(title=title, show_lines=False)
    table.add_column("Rank", justify="right", style="cyan", no_wrap=True)
    table.add_column("Score", justify="right", style="magenta")
    table.add_column("Snippet", overflow="fold")
    for idx, hit in enumerate(hits, start=1):
        score = hit.get("score")
        score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
        snippet = (hit.get("text") or hit.get("title") or "").strip().replace("\n", " ")
        if len(snippet) > 240:
            snippet = snippet[:237] + "…"
        table.add_row(str(idx), score_str, snippet or "(empty)")
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
