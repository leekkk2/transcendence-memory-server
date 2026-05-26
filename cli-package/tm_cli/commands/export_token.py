"""`tm export-token` — print a base64 token built from the current config."""

from __future__ import annotations

import typer

from ..config import encode_connection_token
from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_export_token(state: GlobalState, mode) -> None:
    settings = state.settings()
    endpoint = settings.require_endpoint()
    container = settings.require_container()
    api_key = settings.require_api_key()
    token = encode_connection_token(endpoint=endpoint, container=container, api_key=api_key)
    if mode.json:
        emit_json(
            {
                "token": token,
                "endpoint": endpoint,
                "container": container,
            }
        )
        return
    if mode.quiet:
        return
    typer.echo(token)


def register(app: typer.Typer) -> None:
    @app.command(
        "export-token",
        help="Emit a base64 connection token built from the local config (for device migration).",
    )
    def _export_token(ctx: typer.Context) -> None:
        run(_do_export_token, ctx)
