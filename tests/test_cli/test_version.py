"""`tm version` smoke test — runs offline (no config required)."""

from __future__ import annotations

import json
from tm_cli import __version__

from typer.testing import CliRunner


def test_version_prints_cli_string():
    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert f"transcendence-memory-cli {__version__}" in result.stdout


def test_version_json():
    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "version"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["cli"] == f"transcendence-memory-cli {__version__}"
