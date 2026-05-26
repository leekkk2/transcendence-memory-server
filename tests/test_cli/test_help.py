"""`tm --help` smoke test: every documented subcommand must appear."""

from __future__ import annotations

from typer.testing import CliRunner

_EXPECTED = [
    "connect",
    "status",
    "search",
    "remember",
    "update",
    "delete",
    "embed",
    "query",
    "upload",
    "containers",
    "batch",
    "export-token",
    "config",
    "version",
]


def test_help_lists_every_subcommand():
    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for cmd in _EXPECTED:
        assert cmd in result.stdout, f"missing subcommand in help: {cmd}"
