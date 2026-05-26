"""`tm update` — update an existing memory object's text (and optional fields)."""

from __future__ import annotations

from typing import Optional

import typer

from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_update(
    state: GlobalState,
    mode,
    memory_id: str,
    text: Optional[str],
    title: Optional[str],
    source: Optional[str],
    tags: Optional[str],
) -> None:
    settings = state.settings()
    container = settings.require_container()
    body: dict = {}
    if text is not None:
        body["text"] = text
    if title is not None:
        body["title"] = title
    if source is not None:
        body["source"] = source
    if tags is not None:
        body["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if not body:
        raise typer.BadParameter("Pass at least one of --text/--title/--source/--tags.")
    with state.client() as client:
        result = client.put(
            f"/containers/{container}/memories/{memory_id}",
            json_body=body,
        )
    if not isinstance(result, dict):
        result = {"updated": False, "raw": result}
    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    typer.echo(
        f"Updated {memory_id} in {container} (updated={result.get('updated')}). "
        f"{result.get('index_hint', '')}"
    )


def register(app: typer.Typer) -> None:
    @app.command("update", help="Update an existing memory object (text/tags/etc).")
    def _update(
        ctx: typer.Context,
        memory_id: str = typer.Argument(..., help="Memory object id."),
        text: Optional[str] = typer.Argument(None, help="New text body (omit to keep)."),
        title: Optional[str] = typer.Option(None, "--title"),
        source: Optional[str] = typer.Option(None, "--source"),
        tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tag list."),
    ) -> None:
        run(_do_update, ctx, memory_id, text, title, source, tags)
