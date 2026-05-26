"""Shared fixtures for the ``tm`` CLI test suite.

Strategy:
* Put ``cli-package/`` on ``sys.path`` so ``import tm_cli`` works without
  installing the wheel.
* Per-test temp ``HOME`` so config-file writes never escape the sandbox.
  Avoid ``importlib.reload`` — reloading ``tm_cli.config`` would mint fresh
  ``ConfigMissingError`` / ``Settings`` classes that the already-imported
  ``tm_cli.runner`` would no longer catch by isinstance.
* Mock backend — register routes as a dict of ``"METHOD /path" → (status, body)``
  wired through ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CLI_PKG = _REPO_ROOT / "cli-package"
if str(_CLI_PKG) not in sys.path:
    sys.path.insert(0, str(_CLI_PKG))


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Redirect ``$HOME`` so config writes go to a tmp directory.

    Monkeypatches the on-module CONFIG_PATH / CONFIG_DIR instead of reloading
    ``tm_cli.config`` (reloading would break ``isinstance`` checks downstream).
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    import tm_cli.config as cfg

    new_dir = tmp_path / ".transcendence-memory"
    new_path = new_dir / "config.toml"
    monkeypatch.setattr(cfg, "CONFIG_DIR", new_dir)
    monkeypatch.setattr(cfg, "CONFIG_PATH", new_path)
    return tmp_path


@pytest.fixture
def make_transport() -> Callable:
    """Return a builder that wires up an ``httpx.MockTransport``.

    Usage::

        transport = make_transport({"GET /health": (200, {"status": "ok"})})
    """

    def _builder(routes: dict[str, tuple[int, object]]):
        def handler(request: httpx.Request) -> httpx.Response:
            key = f"{request.method} {request.url.path}"
            if key not in routes:
                return httpx.Response(404, json={"detail": f"unmocked: {key}"})
            status, body = routes[key]
            if isinstance(body, (dict, list)):
                return httpx.Response(
                    status,
                    content=json.dumps(body).encode("utf-8"),
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(status, content=str(body).encode("utf-8"))

        return httpx.MockTransport(handler)

    return _builder


@pytest.fixture
def write_test_config(_isolated_home):
    """Write a baseline ``config.toml`` for tests that need a configured CLI."""

    def _write(endpoint: str = "https://your-rag.example.com", container: str = "your-project", api_key: str = "test-key"):
        from tm_cli.config import write_config

        # Use the patched CONFIG_PATH so writes land in the tmp HOME.
        import tm_cli.config as cfg

        return write_config(endpoint=endpoint, container=container, api_key=api_key, config_path=cfg.CONFIG_PATH)

    return _write
