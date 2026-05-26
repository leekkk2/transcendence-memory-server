"""`tm status` hits ``/health`` and renders / returns the payload."""

from __future__ import annotations

import importlib
import json

from typer.testing import CliRunner


def _patch_transport(monkeypatch, transport):
    """Patch ``Client`` so every command uses our mock transport."""

    import tm_cli.runner as runner_mod
    from tm_cli.client import Client
    from tm_cli.config import Settings

    real_init = Client.__init__

    def _wrapped_init(self, settings: Settings, *, transport=None, verbose=False, timeout=30.0):  # noqa: D401
        real_init(self, settings, transport=transport, verbose=verbose, timeout=timeout)

    def _patched_client(self, *, transport=None):
        return Client(self.settings(), transport=transport, verbose=self.verbose)

    monkeypatch.setattr(runner_mod.GlobalState, "client", lambda self, *, transport=transport: Client(self.settings(), transport=transport, verbose=self.verbose))


def test_status_json(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport(
        {
            "GET /health": (
                200,
                {
                    "status": "ok",
                    "service": "transcendence-memory v0.17.0",
                    "build_flavor": "full",
                    "multimodal_capable": True,
                    "warnings": [],
                },
            )
        }
    )
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["health"]["status"] == "ok"


def test_status_auth_failure_exit_code(monkeypatch, write_test_config, make_transport):
    write_test_config()
    transport = make_transport({"GET /health": (401, {"detail": "Unauthorized"})})
    _patch_transport(monkeypatch, transport)

    from tm_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 2, result.output
    assert "Authentication failed" in result.stderr or "Authentication failed" in result.output
