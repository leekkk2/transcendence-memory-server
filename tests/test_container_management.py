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


def test_create_and_rename_container_by_id(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # 1. 创建容器：自动分配不可变 ID，并绑定 name 为主别名
    create_resp = client.post(
        "/containers",
        json={"name": "alpha-box", "description": "Alpha service container"},
        headers=auth_headers(),
    )
    assert create_resp.status_code == 200
    create_data = create_resp.json()
    assert create_data["name"] == "alpha-box"
    cid = create_data["id"]
    assert cid.startswith("cnt_")
    assert (workspace / "tasks" / "rag" / "containers" / cid).is_dir()

    # 2. 防重名冲突：再次尝试以 alpha-box 创建容器报错 409
    dup_resp = client.post(
        "/containers",
        json={"name": "alpha-box"},
        headers=auth_headers(),
    )
    assert dup_resp.status_code == 409

    # 3. 创建第二个容器 beta-box
    create_beta = client.post(
        "/containers",
        json={"name": "beta-box"},
        headers=auth_headers(),
    )
    assert create_beta.status_code == 200
    beta_cid = create_beta.json()["id"]
    assert beta_cid != cid

    # 4. 通过 ID 将 alpha-box 改名为 alpha-renamed
    rename_resp = client.post(
        f"/containers/{cid}/rename",
        json={"new_name": "alpha-renamed"},
        headers=auth_headers(),
    )
    assert rename_resp.status_code == 200
    rename_data = rename_resp.json()
    assert rename_data["id"] == cid
    assert rename_data["old_name"] == "alpha-box"
    assert rename_data["new_name"] == "alpha-renamed"
    assert rename_data["renamed"] is True

    # 5. 验证底层的物理目录依然是不可变的 cid，未发生物理重命名
    assert (workspace / "tasks" / "rag" / "containers" / cid).is_dir()

    # 6. 改名冲突检测：尝试将 alpha 容器改名为已被 beta 容器占用的 beta-box，应被 409 拦截
    conflict_resp = client.post(
        f"/containers/{cid}/rename",
        json={"new_name": "beta-box"},
        headers=auth_headers(),
    )
    assert conflict_resp.status_code == 409

    # 7. PUT 方法支持，通过当前名称进行改名
    put_resp = client.put(
        "/containers/alpha-renamed/rename",
        json={"new_name": "alpha-final"},
        headers=auth_headers(),
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["id"] == cid
    assert put_resp.json()["new_name"] == "alpha-final"

    # 8. 自身改名相同报错 400
    same_resp = client.post(
        f"/containers/{cid}/rename",
        json={"new_name": "alpha-final"},
        headers=auth_headers(),
    )
    assert same_resp.status_code == 400

    # 9. 不存在容器改名报错 404
    missing_resp = client.post(
        "/containers/non_existent/rename",
        json={"new_name": "whatever"},
        headers=auth_headers(),
    )
    assert missing_resp.status_code == 404

    # 10. GET /containers 列表中应体现不可变 ID 与主名称
    list_resp = client.get("/containers", headers=auth_headers())
    assert list_resp.status_code == 200
    items = list_resp.json()["containers"]
    alpha_item = next((it for it in items if it["id"] == cid), None)
    assert alpha_item is not None
    assert alpha_item["name"] == "alpha-final"
    assert "alpha-renamed" in alpha_item["aliases"] or "alpha-final" in alpha_item["aliases"]
