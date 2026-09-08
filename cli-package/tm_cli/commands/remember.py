"""`tm remember` — quickly write a single memory object."""

from __future__ import annotations

import time
import uuid
from typing import Optional

import typer

from ..formatters import emit_json
from ..redaction import redact
from ..runner import GlobalState, run


def _do_remember(
    state: GlobalState,
    mode,
    text: str,
    tags: Optional[str],
    title: Optional[str],
    source: Optional[str],
    auto_embed: bool,
    verify: bool = False,
    verify_timeout: int = 60,
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

    obj = redact(obj)
    payload = {"container": container, "objects": [obj], "auto_embed": auto_embed}
    with state.client() as client:
        result = client.post("/ingest-memory/objects", json_body=payload)
    if not isinstance(result, dict):
        result = {"accepted": 0, "raw": result}

    from ..receipts import persist_receipt
    receipt_path = persist_receipt(result,container,obj_id)
    if verify:
        from ..receipts import verify_receipt
        with state.client() as client:
            result['verification'] = verify_receipt(client,result,container,obj,verify_timeout)
    if mode.json:
        emit_json({"id": obj_id, "container": container, "result": result})
        return
    if mode.quiet:
        return
    typer.echo(
        f"Stored {obj_id} in {container} receipt={receipt_path} "
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
        verify: bool = typer.Option(False,"--verify",help="Wait for this receipt, then verify its original source; never resend a write."),
        verify_timeout: int = typer.Option(60,"--verify-timeout",min=1,max=300),
        auto_embed: bool = typer.Option(
            True,
            "--auto-embed/--no-auto-embed",
            help="Trigger background embed after ingest (default on).",
        ),
    ) -> None:
        run(_do_remember, ctx, text, tags, title, source, auto_embed, verify, verify_timeout)
