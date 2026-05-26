"""`tm remember` — quickly write a single memory object."""

from __future__ import annotations

import time
import uuid
from typing import Optional

import typer

from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_remember(
    state: GlobalState,
    mode,
    text: str,
    tags: Optional[str],
    title: Optional[str],
    source: Optional[str],
    auto_embed: bool,
) -> None:
    settings = state.settings()
    container = settings.require_container()
    obj_id = f"mem-tm-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    obj = {
        "id": obj_id,
        "text": text,
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
    }
    if title:
        obj["title"] = title
    if source:
        obj["source"] = source

    payload = {"container": container, "objects": [obj], "auto_embed": auto_embed}
    with state.client() as client:
        result = client.post("/ingest-memory/objects", json_body=payload)
    if not isinstance(result, dict):
        result = {"accepted": 0, "raw": result}

    if mode.json:
        emit_json({"id": obj_id, "container": container, "result": result})
        return
    if mode.quiet:
        return
    typer.echo(
        f"Stored {obj_id} in {container} "
        f"(accepted={result.get('accepted')}). "
        f"{result.get('index_hint', '')}"
    )


def register(app: typer.Typer) -> None:
    @app.command("remember", help="Store a single memory in the configured container.")
    def _remember(
        ctx: typer.Context,
        text: str = typer.Argument(..., help="Memory body text."),
        tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tag list."),
        title: Optional[str] = typer.Option(None, "--title", help="Optional title."),
        source: Optional[str] = typer.Option(None, "--source", help="Optional source identifier."),
        auto_embed: bool = typer.Option(
            True,
            "--auto-embed/--no-auto-embed",
            help="Trigger background embed after ingest (default on).",
        ),
    ) -> None:
        run(_do_remember, ctx, text, tags, title, source, auto_embed)
