"""`tm containers` — list containers + index state."""

from __future__ import annotations

from typing import Optional

import typer

from ..formatters import build_console, emit_json, render_containers_table
from ..runner import GlobalState, run


def _do_containers(
    state: GlobalState,
    mode,
    pattern: Optional[str],
    pattern_mode: str,
) -> None:
    params: dict = {}
    if pattern:
        params["pattern"] = pattern
        params["mode"] = pattern_mode
    with state.client() as client:
        result = client.get("/containers", params=params)
    if not isinstance(result, dict):
        result = {"containers": [], "raw": result}

    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    console = build_console(mode)
    rows = result.get("containers") or []
    render_containers_table(rows, console=console)
    console.print(f"\ntotal: {result.get('count', len(rows))}")


def register(app: typer.Typer) -> None:
    @app.command("containers", help="List containers (optionally pattern-filtered).")
    def _containers(
        ctx: typer.Context,
        pattern: Optional[str] = typer.Argument(None, help="Case-insensitive name pattern."),
        pattern_mode: str = typer.Option(
            "substring", "--pattern-mode", help="substring / prefix / glob"
        ),
    ) -> None:
        run(_do_containers, ctx, pattern, pattern_mode)
