"""Tests for /export-connection-token endpoint."""
from __future__ import annotations

import base64
import json
from pathlib import Path

from conftest import API_KEY, DEFAULT_CONTAINER, auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


def test_export_token_returns_valid_base64(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    monkeypatch.setenv("RAG_ADVERTISED_ENDPOINT", "https://example.com:8711")
    server = load_server(workspace, monkeypatch, {"RAG_ADVERTISED_ENDPOINT": "https://example.com:8711"})
    client = TestClient(server.app)

    resp = client.get("/export-connection-token", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()

    assert body["endpoint"] == "https://example.com:8711"
    assert body["container"] == "imac"  # 默认 container

    # token 是有效的 base64
    decoded = json.loads(base64.b64decode(body["token"]).decode("utf-8"))
    assert decoded["endpoint"] == "https://example.com:8711"
    assert decoded["api_key"] == API_KEY
    assert decoded["container"] == "imac"


def test_export_token_custom_container(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get("/export-connection-token?container=mybox", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["container"] == "mybox"

    decoded = json.loads(base64.b64decode(body["token"]).decode("utf-8"))
    assert decoded["container"] == "mybox"
