"""`tm connect` — import a connection token (or run an interactive prompt)."""

from __future__ import annotations

from typing import Optional

import typer

from ..config import CONFIG_PATH, decode_connection_token, write_config
from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_connect(
    state: GlobalState,
    mode,
    token: Optional[str],
    manual: bool,
) -> None:
    if manual:
        endpoint = typer.prompt("Endpoint (e.g. https://your-rag.example.com)")
        container = typer.prompt("Container", default="default")
        api_key = typer.prompt("API key", hide_input=True)
    else:
        if not token:
            raise typer.BadParameter("Pass a connection token or use --manual.")
        payload = decode_connection_token(token)
        endpoint = payload["endpoint"]
        container = payload["container"]
        api_key = payload["api_key"]

    path = write_config(endpoint=endpoint, container=container, api_key=api_key)
    summary = {
        "endpoint": endpoint,
        "container": container,
        "config_path": str(path),
        "auth_mode": "api_key",
    }
    if mode.json:
        emit_json(summary)
    elif not mode.quiet:
        typer.echo(f"Saved connection settings to {path}")
        typer.echo(f"  endpoint  = {endpoint}")
        typer.echo(f"  container = {container}")


def register(app: typer.Typer) -> None:
    @app.command("connect", help="Import a connection token or paste settings manually.")
    def _connect(
        ctx: typer.Context,
        token: Optional[str] = typer.Argument(None, help="Base64 connection token from /export-connection-token."),
        manual: bool = typer.Option(False, "--manual", help="Prompt for endpoint / container / api-key interactively."),
    ) -> None:
        run(_do_connect, ctx, token, manual)
