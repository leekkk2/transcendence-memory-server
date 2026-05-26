"""`tm batch` — JSONL ingest, mirrors the skill's ``batch-ingest.py`` semantics."""

from __future__ import annotations

from pathlib import Path

import typer

from .. import batch_core as bc
from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_batch(
    state: GlobalState,
    mode,
    input_file: Path,
    redact: bool,
    resume: bool,
    batch_size: int,
    max_bytes: int,
    auto_embed: bool,
) -> None:
    settings = state.settings()
    container = settings.require_container()
    if not input_file.exists():
        raise typer.BadParameter(f"input file not found: {input_file}")

    progress_file = bc.progress_file_for(input_file)
    failed_log = bc.failed_log_for(input_file)
    start_line = 0
    if resume and progress_file.exists():
        try:
            start_line = int(progress_file.read_text().strip())
        except (ValueError, OSError):
            start_line = 0

    result = bc.BatchResult()
    batch: list[dict] = []
    batch_bytes = 0
    last_line = start_line

    # Use the same httpx client the rest of the CLI uses (with WAF-friendly UA).
    with state.client(transport=None) as cli_client:
        # `cli_client._client` is the underlying httpx.Client; we hand it to the
        # batch core helpers which need raw HTTP semantics (413 split etc.).
        httpx_client = cli_client._client  # noqa: SLF001 — intentional bridge.
        with open(failed_log, "a", encoding="utf-8") as failed_fh:
            for line_no, obj in bc.iter_jsonl(input_file, start_line=start_line):
                if redact and isinstance(obj.get("text"), str):
                    obj["text"] = bc.redact_text(obj["text"])
                obj_bytes = bc.estimate_payload_bytes([obj])
                if batch and (len(batch) >= batch_size or batch_bytes + obj_bytes > max_bytes):
                    _flush(httpx_client, container, batch, result, failed_fh, auto_embed=auto_embed)
                    batch = []
                    batch_bytes = 0
                    progress_file.write_text(str(line_no - 1))
                batch.append(obj)
                batch_bytes += obj_bytes
                last_line = line_no
            if batch:
                _flush(httpx_client, container, batch, result, failed_fh, auto_embed=auto_embed)
                progress_file.write_text(str(last_line))

    if progress_file.exists():
        try:
            progress_file.unlink()
        except OSError:
            pass

    summary = {
        "container": container,
        "sent": result.sent,
        "accepted": result.accepted,
        "skipped_413": result.skipped,
        "failed": len(result.failed),
        "failed_log": str(failed_log) if result.failed else None,
    }
    if mode.json:
        emit_json(summary)
        return
    if mode.quiet:
        return
    typer.echo(
        f"batch done: sent={result.sent} accepted={result.accepted} "
        f"skipped(413)={result.skipped} failed={len(result.failed)}"
    )
    if result.failed:
        typer.echo(f"  see {failed_log}")
    typer.echo("hint: `tm embed --container-name <name>` to refresh the index")


def _flush(httpx_client, container, batch, result, failed_fh, *, auto_embed) -> None:
    try:
        resp = bc.send_batch(httpx_client, container, batch, auto_embed=auto_embed)
    except Exception as exc:
        for obj in batch:
            obj_id = str(obj.get("id", "unknown"))
            result.failed.append(obj_id)
            failed_fh.write(
                _json_line({"id": obj_id, "reason": str(exc)[:200]})
            )
        result.sent += len(batch)
        return
    result.sent += len(batch)
    result.accepted += int(resp.get("accepted", 0) or 0)
    skipped = resp.get("skipped_413") or []
    result.skipped += len(skipped)
    for obj_id in skipped:
        result.failed.append(str(obj_id))
        failed_fh.write(_json_line({"id": obj_id, "reason": "413_too_large"}))


def _json_line(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False) + "\n"


def register(app: typer.Typer) -> None:
    @app.command("batch", help="Bulk ingest a JSONL file into the configured container.")
    def _batch(
        ctx: typer.Context,
        input_file: Path = typer.Argument(..., help="JSONL file with one object per line."),
        redact: bool = typer.Option(False, "--redact", help="Scrub common secret patterns before ingest."),
        resume: bool = typer.Option(False, "--resume", help="Skip lines already processed on a previous run."),
        batch_size: int = typer.Option(bc.DEFAULT_BATCH_SIZE, "--batch-size", min=1),
        max_bytes: int = typer.Option(bc.DEFAULT_MAX_BYTES, "--max-bytes", min=1024),
        auto_embed: bool = typer.Option(False, "--auto-embed", help="Trigger embed after each batch (default off)."),
    ) -> None:
        run(_do_batch, ctx, input_file, redact, resume, batch_size, max_bytes, auto_embed)
