"""`tm embed` — rebuild the LanceDB index for a container."""

from __future__ import annotations

from typing import Optional

import typer

from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_embed(
    state: GlobalState,
    mode,
    container: Optional[str],
    background: bool,
) -> None:
    settings = state.settings()
    target = container or settings.require_container()
    body = {"container": target, "wait": not background}
    with state.client() as client:
        result = client.post("/embed", json_body=body)
    if not isinstance(result, dict):
        result = {"raw": result}
    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    typer.echo(
        f"Embed dispatched for {target} (code={result.get('code')}, "
        f"status={result.get('status')}). {result.get('note') or ''}"
    )


def register(app: typer.Typer) -> None:
    @app.command("embed", help="Trigger a re-embed for a container (default: configured one).")
    def _embed(
        ctx: typer.Context,
        container: Optional[str] = typer.Option(None, "--container-name", help="Target container (defaults to configured)."),
        background: bool = typer.Option(False, "--async", help="Return immediately without waiting."),
    ) -> None:
        run(_do_embed, ctx, container, background)
