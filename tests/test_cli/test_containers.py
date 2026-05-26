"""`tm containers` lists containers from ``/containers``."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from .test_status import _patch_transport


def test_containers_json(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport(
        {
            "GET /containers": (
                200,
                {
                    "containers": [
                        {
                            "name": "your-project",
                            "objects": 1234,
                            "indexed": True,
                            "last_modified": "2026-05-26T14:32:00Z",
                            "index_state": "fresh",
                            "metadata": None,
                            "aliases": [],
                        },
                        {
                            "name": "team-alpha",
                            "objects": 12,
                            "indexed": False,
                            "last_modified": None,
                            "index_state": "stale",
                            "metadata": None,
                            "aliases": [],
                        },
                    ],
                    "count": 2,
                },
            )
        }
    )
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "containers"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    names = [c["name"] for c in payload["containers"]]
    assert names == ["your-project", "team-alpha"]
