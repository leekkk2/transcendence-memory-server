"""`tm version` — show CLI version (+ server version when configured)."""

from __future__ import annotations

import typer

from .. import __version__
from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_version(state: GlobalState, mode) -> None:
    server_version: str | None = None
    settings = state.settings()
    if settings.endpoint and settings.api_key:
        try:
            with state.client() as client:
                health = client.get("/health")
                if isinstance(health, dict):
                    server_version = (
                        health.get("service")
                        or health.get("version")
                        or None
                    )
        except Exception:
            server_version = None
    payload = {
        "cli": f"transcendence-memory-cli {__version__}",
        "server": server_version,
        "endpoint": settings.endpoint,
    }
    if mode.json:
        emit_json(payload)
        return
    if mode.quiet:
        return
    typer.echo(payload["cli"])
    if server_version:
        typer.echo(f"server: {server_version} @ {settings.endpoint}")


def register(app: typer.Typer) -> None:
    @app.command("version", help="Print transcendence-memory-cli version (+ server when reachable).")
    def _version(ctx: typer.Context) -> None:
        run(_do_version, ctx)
