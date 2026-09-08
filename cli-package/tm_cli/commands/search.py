"""`tm search` — semantic search within / across containers."""

from __future__ import annotations

from typing import Optional

import typer

from ..formatters import build_console, emit_json, render_search_hits
from ..runner import GlobalState, run


def _do_search(
    state: GlobalState,
    mode,
    query: str,
    topk: int,
    all_containers: bool,
    match: Optional[str],
    pattern_mode: str,
    rerank: Optional[bool] = None,
    max_distance: Optional[float] = None,
    full: bool = False,
) -> None:
    settings = state.settings()
    body: dict = {"query": query, "topk": topk}
    if all_containers:
        body["container_pattern"] = "*"
        body["pattern_mode"] = "glob"
    elif match:
        body["container_pattern"] = match
        body["pattern_mode"] = pattern_mode
    else:
        body["container"] = settings.require_container()

    if rerank is not None: body['rerank']=rerank
    if max_distance is not None: body['score_threshold']=max_distance
    with state.client() as client:
        result = client.post("/search", json_body=body)
    if not isinstance(result, dict):
        result = {"results": [], "raw": result}

    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return

    console = build_console(mode)
    hits = result.get('results') or []
    console.print(f"rerank_applied={result.get('rerank_applied',False)} degraded={result.get('is_degraded',result.get('degraded',False))}")
    render_search_hits(
        hits,
        console=console,
        query=query,
        container=(result.get("container") or settings.container or "?"),
        topk=topk,
        full=full,
    )


def register(app: typer.Typer) -> None:
    @app.command("search", help="Search the configured container (or use --all / --match).")
    def _search(
        ctx: typer.Context,
        query: str = typer.Argument(..., help="Search query."),
        topk: int = typer.Option(5, "--topk", "-k", min=1, max=100, help="Number of hits to return."),
        all_containers: bool = typer.Option(False, "--all", help="Search across every container."),
        match: Optional[str] = typer.Option(None, "--match", help="Container name pattern."),
        rerank: Optional[bool] = typer.Option(None,"--rerank/--no-rerank"),
        max_distance: Optional[float] = typer.Option(None,"--max-distance",help="Squared L2 upper bound; <=0 disables filtering."),
        full: bool = typer.Option(False,"--full",help="Print complete source text."),
        pattern_mode: str = typer.Option(
            "substring",
            "--pattern-mode",
            help="Pattern mode used with --match: substring / prefix / glob.",
        ),
    ) -> None:
        run(_do_search, ctx, query, topk, all_containers, match, pattern_mode, rerank, max_distance, full)
