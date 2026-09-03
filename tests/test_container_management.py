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


def test_rename_container(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # 准备原容器
    target = workspace / "tasks" / "rag" / "containers" / "old_box"
    target.mkdir(parents=True)
    (target / "memory_objects.jsonl").write_text('{"id":"m1","text":"hello"}\n')

    # 1. 成功重命名
    resp = client.post(
        "/containers/old_box/rename",
        json={"new_name": "new_box"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["old_name"] == "old_box"
    assert body["new_name"] == "new_box"
    assert body["renamed"] is True
    assert not target.exists()
    assert (workspace / "tasks" / "rag" / "containers" / "new_box").exists()
    assert (workspace / "tasks" / "rag" / "containers" / "new_box" / "memory_objects.jsonl").exists()

    # 2. PUT 方法同样支持
    resp_put = client.put(
        "/containers/new_box/rename",
        json={"new_name": "new_box_put"},
        headers=auth_headers(),
    )
    assert resp_put.status_code == 200
    assert resp_put.json()["new_name"] == "new_box_put"
    assert (workspace / "tasks" / "rag" / "containers" / "new_box_put").exists()

    # 3. 目标已存在报错 409
    (workspace / "tasks" / "rag" / "containers" / "another_box").mkdir(parents=True)
    resp = client.post(
        "/containers/new_box_put/rename",
        json={"new_name": "another_box"},
        headers=auth_headers(),
    )
    assert resp.status_code == 409

    # 4. 源不存在报错 404
    resp = client.post(
        "/containers/non_existent/rename",
        json={"new_name": "some_box"},
        headers=auth_headers(),
    )
    assert resp.status_code == 404

    # 5. 新旧名称相同报错 400
    resp = client.post(
        "/containers/new_box_put/rename",
        json={"new_name": "new_box_put"},
        headers=auth_headers(),
    )
    assert resp.status_code == 400
