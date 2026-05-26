"""`tm query` — RAG-Anything natural-language answer over a container."""

from __future__ import annotations

import typer

from ..formatters import build_console, emit_json
from ..runner import GlobalState, run


def _do_query(state: GlobalState, mode, question: str, mode_param: str, top_k: int) -> None:
    settings = state.settings()
    container = settings.require_container()
    body = {"query": question, "container": container, "mode": mode_param, "top_k": top_k}
    with state.client() as client:
        result = client.post("/query", json_body=body)
    if not isinstance(result, dict):
        result = {"answer": str(result)}
    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    console = build_console(mode)
    console.print(f"[bold]container[/bold]: {result.get('container', container)}")
    console.print(f"[bold]mode[/bold]     : {result.get('mode', mode_param)}")
    console.print()
    console.print(result.get("answer") or "(no answer)")


def register(app: typer.Typer) -> None:
    @app.command("query", help="Run a RAG-Anything natural-language query.")
    def _query(
        ctx: typer.Context,
        question: str = typer.Argument(..., help="Natural-language question."),
        mode_param: str = typer.Option("hybrid", "--mode", help="RAG mode: hybrid / local / global / naive."),
        top_k: int = typer.Option(60, "--top-k", min=1, max=500, help="Top-k chunks to consider."),
    ) -> None:
        run(_do_query, ctx, question, mode_param, top_k)
