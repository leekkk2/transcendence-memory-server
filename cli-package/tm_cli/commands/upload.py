"""`tm upload` — multimodal document ingest (PDF / Office / image / md)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_upload(
    state: GlobalState,
    mode,
    file: Path,
    parse_method: Optional[str],
) -> None:
    settings = state.settings()
    container = settings.require_container()
    if not file.exists():
        raise typer.BadParameter(f"file not found: {file}")
    data = {"container": container}
    if parse_method:
        data["parse_method"] = parse_method
    with state.client() as client:
        with open(file, "rb") as fh:
            files = {"file": (file.name, fh, "application/octet-stream")}
            result = client.post("/documents/upload", data=data, files=files)
    if not isinstance(result, dict):
        result = {"raw": result}
    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    typer.echo(
        f"Uploaded {file.name} to {container} (code={result.get('code')}, "
        f"status={result.get('status')}). Poll GET /jobs/<pid> for progress."
    )


def register(app: typer.Typer) -> None:
    @app.command("upload", help="Upload a document (PDF/Office/image/md) to the configured container.")
    def _upload(
        ctx: typer.Context,
        file: Path = typer.Argument(..., help="File to upload."),
        parse_method: Optional[str] = typer.Option(None, "--parse-method", help="Override the parser (mineru / native / ...)."),
    ) -> None:
        run(_do_upload, ctx, file, parse_method)
