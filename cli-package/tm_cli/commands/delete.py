"""`tm delete` — delete a memory object by id."""

from __future__ import annotations

import typer

from ..formatters import emit_json
from ..runner import GlobalState, run


def _do_delete(state: GlobalState, mode, memory_id: str, yes: bool) -> None:
    settings = state.settings()
    container = settings.require_container()
    if not yes and not mode.quiet:
        typer.confirm(f"Delete memory {memory_id} from {container}?", abort=True)
    with state.client() as client:
        result = client.delete(f"/containers/{container}/memories/{memory_id}")
    if not isinstance(result, dict):
        result = {"deleted": False, "raw": result}
    if mode.json:
        emit_json(result)
        return
    if mode.quiet:
        return
    typer.echo(
        f"Deleted {memory_id} from {container} (deleted={result.get('deleted')}). "
        f"{result.get('message', '')}"
    )


def register(app: typer.Typer) -> None:
    @app.command("delete", help="Delete a memory object by id.")
    def _delete(
        ctx: typer.Context,
        memory_id: str = typer.Argument(...),
        yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    ) -> None:
        run(_do_delete, ctx, memory_id, yes)
