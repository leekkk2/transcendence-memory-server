"""Typer entrypoint for the ``tm`` CLI.

Wires the global options (``--endpoint`` / ``--container`` / ...) onto the
typer ``Context`` so each subcommand can resolve runtime settings via the
shared :mod:`tm_cli.config` machinery.
"""

from __future__ import annotations

from typing import Optional

import typer

from . import __version__
from .runner import GlobalState

app = typer.Typer(
    name="tm",
    add_completion=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help=(
        "Shell CLI for the transcendence-memory self-hosted RAG memory service. "
        "Pair once with `tm connect <token>`, then `tm search`, `tm remember`, "
        "`tm query` from any terminal."
    ),
)


@app.callback()
def _root(
    ctx: typer.Context,
    endpoint: Optional[str] = typer.Option(None, "--endpoint", help="Override the configured server endpoint."),
    container: Optional[str] = typer.Option(None, "--container", "-c", help="Override the configured container."),
    api_key: Optional[str] = typer.Option(None, "--api-key", help="Override the configured API key (scripts only)."),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON (jq-friendly)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress stdout; rely on the exit code."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print HTTP request/response traces."),
    no_color: bool = typer.Option(False, "--no-color", help="Disable rich color output (CI-friendly)."),
) -> None:
    ctx.obj = GlobalState(
        endpoint_override=endpoint,
        container_override=container,
        api_key_override=api_key,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
        no_color=no_color,
    )


# ---------------------------------------------------------------------------
# Subcommand registration — each command lives in its own module.
# ---------------------------------------------------------------------------
from .commands import (  # noqa: E402  (deferred import)
    batch as _batch,
    config_cmd as _config_cmd,
    connect as _connect,
    containers as _containers,
    delete as _delete,
    embed as _embed,
    export_token as _export_token,
    query as _query,
    remember as _remember,
    search as _search,
    status as _status,
    update as _update,
    upload as _upload,
    version as _version,
)


for _mod in (
    _connect,
    _status,
    _search,
    _remember,
    _update,
    _delete,
    _embed,
    _query,
    _upload,
    _containers,
    _batch,
    _export_token,
    _config_cmd,
    _version,
):
    _mod.register(app)


if __name__ == "__main__":  # pragma: no cover - script-mode entrypoint
    app()
