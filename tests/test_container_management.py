"""Tests for container management endpoints."""
from __future__ import annotations

from pathlib import Path

from conftest import auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


def test_list_containers(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # 创建一些 container 目录
    containers_root = workspace / "tasks" / "rag" / "containers"
    for name in ["alpha", "beta", "gamma"]:
        (containers_root / name).mkdir(parents=True)

    resp = client.get("/containers", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    names = [c["name"] for c in body["containers"]]
    assert names == ["alpha", "beta", "gamma"]
    # 每个容器应包含详细信息
    for c in body["containers"]:
        assert "objects" in c
        assert "indexed" in c
        assert "last_modified" in c


def test_delete_container(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    target = workspace / "tasks" / "rag" / "containers" / "disposable"
    target.mkdir(parents=True)
    (target / "memory_objects.jsonl").write_text('{"id":"x"}\n')

    resp = client.delete("/containers/disposable", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert not target.exists()


def test_container_name_traversal_rejected(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # 名称中含空格或特殊字符的 container 应被拒绝
    # 注意：含 / 的路径会被 FastAPI 路由层拦截为 404，不会到达 validate_container_name
    for bad_name in ["a b", "foo..bar", ".hidden", "with@at"]:
        resp = client.delete(f"/containers/{bad_name}", headers=auth_headers())
        assert resp.status_code in (400, 422), f"expected rejection for {bad_name!r}, got {resp.status_code}"
