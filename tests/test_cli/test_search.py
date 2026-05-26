"""`tm search` posts to ``/search`` and renders / returns hits."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from .test_status import _patch_transport


def test_search_json(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport(
        {
            "POST /search": (
                200,
                {
                    "status": "ok",
                    "command": [],
                    "code": 0,
                    "query": "docker",
                    "topk": 3,
                    "container": "your-project",
                    "containers": ["your-project"],
                    "per_container_status": {"your-project": "ok"},
                    "initialized": True,
                    "results": [
                        {"score": 0.84, "text": "Docker compose conflict on port 8711"},
                        {"score": 0.79, "text": "Multi-stage Dockerfile keep-storage 2GB"},
                    ],
                    "stdout": "",
                    "stderr": "",
                },
            )
        }
    )
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "search", "docker", "--topk", "3"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["results"]) == 2
    assert payload["results"][0]["score"] == 0.84


def test_search_rich_default(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport(
        {
            "POST /search": (
                200,
                {
                    "status": "ok",
                    "command": [],
                    "code": 0,
                    "query": "docker",
                    "topk": 5,
                    "container": "your-project",
                    "containers": ["your-project"],
                    "per_container_status": {"your-project": "ok"},
                    "initialized": True,
                    "results": [{"score": 0.5, "text": "hello"}],
                    "stdout": "",
                    "stderr": "",
                },
            )
        }
    )
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--no-color", "search", "docker"])
    assert result.exit_code == 0, result.output
    assert "hello" in result.stdout
