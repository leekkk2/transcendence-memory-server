"""`tm connect` reads a token and writes the config."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner


def test_connect_with_token(_isolated_home):
    from tm_cli.config import CONFIG_PATH, encode_connection_token, load_config_file
    from tm_cli.main import app

    token = encode_connection_token(
        endpoint="https://your-rag.example.com",
        container="your-project",
        api_key="***",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["connect", token])
    assert result.exit_code == 0, result.output
    cfg = load_config_file()
    assert cfg["connection"]["endpoint"] == "https://your-rag.example.com"
    assert cfg["connection"]["container"] == "your-project"
    assert cfg["auth"]["api_key"] == "***"


def test_connect_rejects_garbage_token(_isolated_home):
    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["connect", "garbage!!"])
    # exit code is non-zero — config never written
    assert result.exit_code != 0
