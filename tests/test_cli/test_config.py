"""Config loading + token decode tests."""

from __future__ import annotations

import base64
import json

import pytest


def test_resolve_settings_priority(monkeypatch, _isolated_home, write_test_config):
    write_test_config(endpoint="https://your-rag.example.com", container="alpha", api_key="from-file")
    monkeypatch.setenv("TM_API_KEY", "from-env")

    from tm_cli.config import resolve_settings

    # CLI override beats env beats file.
    s = resolve_settings(endpoint="https://override.example.com")
    assert s.endpoint == "https://override.example.com"
    assert s.container == "alpha"  # only file had a value
    assert s.api_key == "from-env"


def test_decode_connection_token_roundtrip():
    from tm_cli.config import decode_connection_token, encode_connection_token

    token = encode_connection_token(
        endpoint="https://your-rag.example.com",
        container="your-project",
        api_key="***",
    )
    payload = decode_connection_token(token)
    assert payload["endpoint"] == "https://your-rag.example.com"
    assert payload["container"] == "your-project"
    assert payload["api_key"] == "***"


def test_decode_connection_token_rejects_garbage():
    from tm_cli.config import decode_connection_token

    with pytest.raises(ValueError):
        decode_connection_token("not-a-valid-token")


def test_write_then_read_config(_isolated_home):
    from tm_cli.config import load_config_file, write_config

    path = write_config(
        endpoint="https://your-rag.example.com",
        container="your-project",
        api_key="***",
    )
    assert path.exists()
    cfg = load_config_file()
    assert cfg["connection"]["endpoint"] == "https://your-rag.example.com"
    assert cfg["auth"]["api_key"] == "***"
