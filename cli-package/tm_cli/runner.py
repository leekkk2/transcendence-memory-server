"""Shared runtime plumbing — global state + exception → exit-code adapter.

Kept in its own module so individual command files can import it without
pulling in :mod:`tm_cli.main` (which imports every command, causing cycles).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import typer

from . import exit_codes
from .client import AuthError, CLIError, Client, ConnectionError_, ServerError
from .config import ConfigMissingError, Settings, resolve_settings
from .formatters import OutputMode, emit_error


@dataclass
class GlobalState:
    endpoint_override: Optional[str]
    container_override: Optional[str]
    api_key_override: Optional[str]
    json_output: bool
    quiet: bool
    verbose: bool
    no_color: bool

    def settings(self) -> Settings:
        return resolve_settings(
            endpoint=self.endpoint_override,
            container=self.container_override,
            api_key=self.api_key_override,
        )

    def output_mode(self) -> OutputMode:
        return OutputMode(json=self.json_output, quiet=self.quiet, no_color=self.no_color)

    def client(self, *, transport=None) -> Client:
        return Client(self.settings(), transport=transport, verbose=self.verbose)


def global_state(ctx: typer.Context) -> GlobalState:
    """Return the :class:`GlobalState` attached to the typer context."""

    state = ctx.obj
    if state is None:
        state = GlobalState(None, None, None, False, False, False, False)
        ctx.obj = state
    return state


def run(func, ctx: typer.Context, *args, **kwargs) -> None:
    """Run ``func(state, mode, *args, **kwargs)`` and map exceptions to exit codes."""

    state = global_state(ctx)
    mode = state.output_mode()
    try:
        func(state, mode, *args, **kwargs)
    except ConfigMissingError as exc:
        emit_error(str(exc), mode=mode)
        raise typer.Exit(exit_codes.CONFIG_MISSING)
    except AuthError as exc:
        emit_error(str(exc), mode=mode)
        raise typer.Exit(exit_codes.AUTH)
    except ConnectionError_ as exc:
        emit_error(str(exc), mode=mode)
        raise typer.Exit(exit_codes.CONNECTION)
    except ServerError as exc:
        emit_error(str(exc), mode=mode)
        raise typer.Exit(exit_codes.SERVER)
    except CLIError as exc:
        emit_error(str(exc), mode=mode)
        raise typer.Exit(exc.exit_code)
