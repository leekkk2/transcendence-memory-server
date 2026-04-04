"""Tests for authentication error scenarios."""
from __future__ import annotations

from pathlib import Path

from conftest import load_server, make_workspace
from fastapi.testclient import TestClient


def test_wrong_api_key(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get("/containers", headers={"X-API-KEY": "wrong-key"})
    assert resp.status_code == 401


def test_missing_api_key(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get("/containers")
    assert resp.status_code == 401
