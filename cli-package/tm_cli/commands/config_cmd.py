"""`tm config show` / `tm config set` — inspect & mutate ``config.toml``."""

from __future__ import annotations

import typer

from ..config import (
    CONFIG_PATH,
    load_config_file,
    resolve_settings,
    write_config,
)
from ..formatters import emit_json
from ..runner import GlobalState, run

config_app = typer.Typer(help="Inspect or mutate ~/.transcendence-memory/config.toml.")


def _do_show(state: GlobalState, mode) -> None:
    raw = load_config_file()
    if mode.json:
        emit_json({"path": str(CONFIG_PATH), "config": raw})
        return
    if mode.quiet:
        return
    typer.echo(f"path: {CONFIG_PATH}")
    if not raw:
        typer.echo("(config file missing — run `tm connect` first)")
        return
    connection = raw.get("connection", {}) if isinstance(raw, dict) else {}
    auth = raw.get("auth", {}) if isinstance(raw, dict) else {}
    typer.echo(f"endpoint  = {connection.get('endpoint', '?')}")
    typer.echo(f"container = {connection.get('container', '?')}")
    typer.echo(f"auth.mode = {auth.get('mode', '?')}")
    typer.echo("api_key   = (hidden)")


def _do_set(state: GlobalState, mode, key: str, value: str) -> None:
    if key not in {"endpoint", "container", "api_key"}:
        raise typer.BadParameter("Settable keys: endpoint, container, api_key.")

    current = resolve_settings()  # uses config file only (no overrides)
    endpoint = current.endpoint or ""
    container = current.container or ""
    api_key = current.api_key or ""

    if key == "endpoint":
        endpoint = value
    elif key == "container":
        container = value
    elif key == "api_key":
        api_key = value

    if not (endpoint and container and api_key):
        raise typer.BadParameter(
            "Cannot write partial config — set endpoint, container, and api_key at least once."
        )

    path = write_config(endpoint=endpoint, container=container, api_key=api_key)
    if mode.json:
        emit_json({"updated": key, "config_path": str(path)})
        return
    if mode.quiet:
        return
    typer.echo(f"updated {key} in {path}")


def register(app: typer.Typer) -> None:
    @config_app.command("show", help="Print the current config.toml contents (api_key masked).")
    def _show(ctx: typer.Context) -> None:
        run(_do_show, ctx)

    @config_app.command("set", help="Set a single config key (endpoint / container / api_key).")
    def _set(
        ctx: typer.Context,
        key: str = typer.Argument(...),
        value: str = typer.Argument(...),
    ) -> None:
        run(_do_set, ctx, key, value)

    app.add_typer(config_app, name="config")
