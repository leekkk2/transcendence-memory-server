"""Config file loading and writing for the ``tm`` CLI.

Layout — fully compatible with the ``transcendence-memory`` skill::

    ~/.transcendence-memory/config.toml

    [connection]
    endpoint = "https://your-rag.example.com"
    container = "your-project"

    [auth]
    mode = "api_key"
    api_key = "***"

Resolution order for any value (highest priority first):

    1. Per-command flag (``--endpoint`` / ``--container`` / ``--api-key``)
    2. Environment variable (``TM_ENDPOINT`` / ``TM_API_KEY`` / ``TM_CONTAINER``)
    3. ``config.toml``
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib as _toml_reader
else:  # pragma: no cover - python 3.10 fallback
    import tomli as _toml_reader  # type: ignore[import-not-found]


CONFIG_DIR = Path.home() / ".transcendence-memory"
CONFIG_PATH = CONFIG_DIR / "config.toml"


@dataclass
class Settings:
    """Resolved CLI runtime settings."""

    endpoint: str | None = None
    container: str | None = None
    api_key: str | None = None
    auth_mode: str = "api_key"

    def require_endpoint(self) -> str:
        if not self.endpoint:
            raise ConfigMissingError(
                "endpoint",
                "No endpoint configured. Run `tm connect <token>` first.",
            )
        return self.endpoint.rstrip("/")

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigMissingError(
                "api_key",
                "No API key configured. Run `tm connect <token>` first.",
            )
        return self.api_key

    def require_container(self) -> str:
        if not self.container:
            raise ConfigMissingError(
                "container",
                "No container configured. Pass --container or run `tm connect <token>`.",
            )
        return self.container


class ConfigMissingError(RuntimeError):
    """Raised when a required setting cannot be resolved."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    """Load the on-disk config; return ``{}`` when the file does not exist."""

    config_path = path or CONFIG_PATH
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as fh:
            return _toml_reader.load(fh)
    except Exception as exc:  # pragma: no cover - corrupt file path
        raise RuntimeError(f"failed to parse {config_path}: {exc}") from exc


def resolve_settings(
    *,
    endpoint: str | None = None,
    container: str | None = None,
    api_key: str | None = None,
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Resolve runtime settings with the documented priority order."""

    environ = env if env is not None else os.environ
    file_cfg = load_config_file(config_path)

    connection = file_cfg.get("connection", {}) if isinstance(file_cfg, dict) else {}
    auth = file_cfg.get("auth", {}) if isinstance(file_cfg, dict) else {}

    resolved_endpoint = (
        endpoint
        or environ.get("TM_ENDPOINT")
        or connection.get("endpoint")
    )
    resolved_container = (
        container
        or environ.get("TM_CONTAINER")
        or connection.get("container")
    )
    resolved_api_key = (
        api_key
        or environ.get("TM_API_KEY")
        or auth.get("api_key")
    )
    auth_mode = auth.get("mode") or "api_key"

    return Settings(
        endpoint=resolved_endpoint.rstrip("/") if resolved_endpoint else None,
        container=resolved_container,
        api_key=resolved_api_key,
        auth_mode=auth_mode,
    )


def decode_connection_token(token: str) -> dict[str, Any]:
    """Decode a base64 connection token emitted by ``/export-connection-token``."""

    try:
        raw = base64.b64decode(token.strip().encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid connection token: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("connection token did not decode to a JSON object")
    for key in ("endpoint", "api_key", "container"):
        if key not in payload:
            raise ValueError(f"connection token missing field: {key}")
    return payload


def write_config(
    *,
    endpoint: str,
    container: str,
    api_key: str,
    auth_mode: str = "api_key",
    config_path: Path | None = None,
) -> Path:
    """Write the resolved settings back to ``config.toml`` (atomic replace)."""

    target = config_path or CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "[connection]\n"
        f'endpoint = "{_toml_escape(endpoint)}"\n'
        f'container = "{_toml_escape(container)}"\n'
        "\n"
        "[auth]\n"
        f'mode = "{_toml_escape(auth_mode)}"\n'
        f'api_key = "{_toml_escape(api_key)}"\n'
    )
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(target)
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover - non-POSIX FS
        pass
    return target


def encode_connection_token(*, endpoint: str, container: str, api_key: str) -> str:
    """Build a base64 connection token from on-disk settings (offline path)."""

    payload = json.dumps(
        {"endpoint": endpoint, "api_key": api_key, "container": container},
        ensure_ascii=False,
    ).encode("utf-8")
    return base64.b64encode(payload).decode("ascii")


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
