"""`tm status` — show endpoint + health summary."""

from __future__ import annotations

import typer

from ..formatters import build_console, emit_json, render_status
from ..runner import GlobalState, run


def _do_status(state: GlobalState, mode) -> None:
    settings = state.settings()
    endpoint = settings.require_endpoint()
    with state.client() as client:
        health = client.get("/health")
    if not isinstance(health, dict):
        health = {"status": "unknown", "raw": health}

    if mode.json:
        emit_json(
            {
                "endpoint": endpoint,
                "container": settings.container,
                "health": health,
            }
        )
        return
    if mode.quiet:
        return
    console = build_console(mode)
    render_status(health, console=console, endpoint=endpoint, container=settings.container)


def register(app: typer.Typer) -> None:
    @app.command("status", help="Show endpoint + /health summary.")
    def _status(ctx: typer.Context) -> None:
        run(_do_status, ctx)
