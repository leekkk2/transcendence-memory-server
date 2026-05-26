"""`tm remember` ingests a single object."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from .test_status import _patch_transport


def test_remember_basic(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport(
        {
            "POST /ingest-memory/objects": (
                200,
                {
                    "container": "your-project",
                    "accepted": 1,
                    "stored_path": "/data/your-project/memory_objects.jsonl",
                    "stored_paths": ["/data/your-project/memory_objects.jsonl"],
                    "index_hint": "Run /embed to refresh.",
                },
            )
        }
    )
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--json", "remember", "Hello world", "--tags", "alpha,beta"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["container"] == "your-project"
    assert payload["result"]["accepted"] == 1


def test_remember_requires_container(monkeypatch, _isolated_home, make_transport):
    transport = make_transport({})
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    # No config written — should fail with CONFIG_MISSING (5).
    result = runner.invoke(app, ["remember", "x"])
    assert result.exit_code == 5, result.output
