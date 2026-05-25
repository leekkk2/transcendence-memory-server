"""Tests for container_aliases — 透明 alias 路由层。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import auth_headers, load_server, make_workspace


# ---- ContainerAliases 单元 CRUD ------------------------------------------


def test_alias_crud_basic(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    assert ca.list_all() == []
    assert ca.resolve("missing") is None

    row = ca.upsert(
        alias="sanva",
        canonical="sanva-personal",
        reason="consolidate",
        status="active",
        notes="历史 12 obj 个人偏好",
    )
    assert row["alias"] == "sanva"
    assert row["canonical"] == "sanva-personal"
    assert row["reason"] == "consolidate"
    assert row["status"] == "active"
    assert row["notes"] == "历史 12 obj 个人偏好"
    assert row["created_at"]
    assert row["updated_at"]

    got = ca.resolve("sanva")
    assert got is not None
    assert got["canonical"] == "sanva-personal"


def test_alias_partial_update_preserves_fields(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    ca.upsert(
        alias="x", canonical="x-canonical", reason="r1", status="active", notes="n1"
    )
    ca.upsert(alias="x", canonical="x-canonical", reason="r2", status="deprecated")
    row = ca.resolve("x")
    assert row["reason"] == "r2"
    assert row["status"] == "deprecated"
    # notes 未传 → 保留原值
    assert row["notes"] == "n1"
    # canonical 必须重传（required field）
    assert row["canonical"] == "x-canonical"


def test_alias_list_all_sorted_and_delete(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    ca.upsert(alias="zeta", canonical="z-canonical")
    ca.upsert(alias="alpha", canonical="a-canonical")
    ca.upsert(alias="middle", canonical="m-canonical")
    aliases = [r["alias"] for r in ca.list_all()]
    assert aliases == ["alpha", "middle", "zeta"]

    assert ca.delete("middle") is True
    assert ca.delete("middle") is False
    assert [r["alias"] for r in ca.list_all()] == ["alpha", "zeta"]


def test_alias_invalid_status_rejected(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    with pytest.raises(ValueError):
        ca.upsert(alias="x", canonical="y", status="bogus")


def test_alias_empty_alias_rejected(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    with pytest.raises(ValueError):
        ca.upsert(alias="", canonical="y")
    assert ca.resolve("") is None
    assert ca.delete("") is False


def test_alias_aliases_for_canonical_filters_removed(tmp_path: Path):
    from scripts.container_aliases import ContainerAliases

    ca = ContainerAliases(str(tmp_path / "lancedb"))
    ca.upsert(alias="a", canonical="c", status="active")
    ca.upsert(alias="b", canonical="c", status="deprecated")
    ca.upsert(alias="c-old", canonical="c", status="removed")
    ca.upsert(alias="d", canonical="other", status="active")
    rows = ca.aliases_for_canonical("c")
    names = sorted(r["alias"] for r in rows)
    assert names == ["a", "b"]
    assert ca.aliases_for_canonical("other") == [
        r for r in ca.list_all() if r["alias"] == "d"
    ]
    assert ca.aliases_for_canonical("") == []


# ---- HTTP endpoints: alias admin ----------------------------------------


def _build_client(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    return workspace, client


def test_post_get_delete_aliases_admin(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    # POST
    resp = client.post(
        "/containers/aliases",
        headers=auth_headers(),
        json={
            "alias": "sanva",
            "canonical": "sanva-personal",
            "reason": "consolidate",
            "status": "active",
            "notes": "历史 12 obj",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["alias"] == "sanva"
    assert body["canonical"] == "sanva-personal"

    # GET list
    resp = client.get("/containers/aliases", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["aliases"][0]["alias"] == "sanva"

    # DELETE
    resp = client.delete("/containers/aliases/sanva", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    # DELETE 404 第二次
    resp = client.delete("/containers/aliases/sanva", headers=auth_headers())
    assert resp.status_code == 404


def test_post_alias_invalid_status_400(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.post(
        "/containers/aliases",
        headers=auth_headers(),
        json={"alias": "x", "canonical": "y", "status": "bogus"},
    )
    assert resp.status_code == 400


def test_post_alias_bad_name_rejected(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.post(
        "/containers/aliases",
        headers=auth_headers(),
        json={"alias": "bad name with space", "canonical": "y"},
    )
    assert resp.status_code == 400


def test_alias_admin_requires_auth(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.get("/containers/aliases")
    assert resp.status_code in (401, 403)
    resp = client.post("/containers/aliases", json={"alias": "x", "canonical": "y"})
    assert resp.status_code in (401, 403)
    resp = client.delete("/containers/aliases/x")
    assert resp.status_code in (401, 403)


# ---- 透传路由：ingest 路径 ---------------------------------------------


def _seed_alias(client, alias: str, canonical: str, status: str = "active"):
    resp = client.post(
        "/containers/aliases",
        headers=auth_headers(),
        json={
            "alias": alias,
            "canonical": canonical,
            "reason": "test",
            "status": status,
        },
    )
    assert resp.status_code == 200, resp.text


def test_ingest_objects_alias_routes_to_canonical(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")

    resp = client.post(
        "/ingest-memory/objects",
        headers=auth_headers(),
        json={
            "container": "sanva",
            "auto_embed": False,
            "objects": [{"id": "alias-test-001", "text": "test via alias"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # response.container 保留客户端入参
    assert body["container"] == "sanva"
    # 物理落盘是 canonical
    canonical_path = (
        workspace / "tasks" / "rag" / "containers" / "sanva-personal" / "memory_objects.jsonl"
    )
    assert canonical_path.exists()
    rows = [
        json.loads(line)
        for line in canonical_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r["id"] == "alias-test-001" for r in rows)

    # 旧 alias 目录不应该被创建（避免 double-write）
    alias_dir = workspace / "tasks" / "rag" / "containers" / "sanva"
    assert not (alias_dir / "memory_objects.jsonl").exists()


def test_ingest_objects_removed_alias_410(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="dava", canonical="sanva-yzjx", status="removed")

    resp = client.post(
        "/ingest-memory/objects",
        headers=auth_headers(),
        json={
            "container": "dava",
            "auto_embed": False,
            "objects": [{"id": "x", "text": "y"}],
        },
    )
    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert detail["error"] == "container_removed"
    assert detail["removed_alias"] == "dava"


def test_canonical_still_works_unaffected(tmp_path: Path, monkeypatch):
    """客户端默认 container=sanva-yzjx（self alias / canonical 自身）必须照常工作。

    这是 P0 验收硬标准 —— 不能因为 alias 层破坏现有客户端默认路径。
    """
    workspace, client = _build_client(tmp_path, monkeypatch)
    # 不注册 sanva-yzjx alias，让它走"未命中 alias → name 本身是 canonical"路径
    resp = client.post(
        "/ingest-memory/objects",
        headers=auth_headers(),
        json={
            "container": "sanva-yzjx",
            "auto_embed": False,
            "objects": [{"id": "normal-001", "text": "normal"}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["container"] == "sanva-yzjx"
    canonical_path = (
        workspace / "tasks" / "rag" / "containers" / "sanva-yzjx" / "memory_objects.jsonl"
    )
    assert canonical_path.exists()


# ---- 透传路由：metadata / dump / containers GET -------------------------


def test_metadata_upsert_via_alias_writes_to_canonical(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")

    resp = client.post(
        "/containers/sanva/metadata",
        headers=auth_headers(),
        json={"description": "via alias", "scope": "sanva", "purpose": "active"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # metadata 行的 name 字段是 canonical
    assert body["name"] == "sanva-personal"
    assert body["description"] == "via alias"


def test_dump_container_via_alias(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")
    # 准备 canonical 数据
    root = workspace / "tasks" / "rag" / "containers" / "sanva-personal"
    root.mkdir(parents=True)
    (root / "memory_objects.jsonl").write_text(
        json.dumps({"id": "a", "text": "x"}) + "\n", encoding="utf-8"
    )

    # via alias 也能 dump 到同一份数据
    resp = client.get("/containers/sanva/dump", headers=auth_headers())
    assert resp.status_code == 200
    line = next(ln for ln in resp.text.split("\n") if ln.strip())
    assert json.loads(line)["id"] == "a"


def test_containers_list_includes_aliases_field(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    # 准备 canonical 物理目录
    root = workspace / "tasks" / "rag" / "containers"
    (root / "sanva-personal").mkdir(parents=True)
    # 注册两个 active alias + 一个 removed alias 指向同一个 canonical
    _seed_alias(client, alias="sanva", canonical="sanva-personal", status="active")
    _seed_alias(client, alias="sanva-old", canonical="sanva-personal", status="deprecated")
    _seed_alias(client, alias="dava", canonical="sanva-personal", status="removed")

    resp = client.get("/containers", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    target = next(c for c in body["containers"] if c["name"] == "sanva-personal")
    assert "aliases" in target
    # active + deprecated 入列，removed 不入列
    assert sorted(target["aliases"]) == ["sanva", "sanva-old"]


# ---- /search 透传：单容器 + containers 数组 + removed silently skip ---


def test_search_single_container_via_alias_uses_canonical(tmp_path: Path, monkeypatch):
    """/search 单容器场景 alias 透传到 canonical，返回时 container 字段保留原名。

    用未初始化（无 LanceDB 表）的 canonical 容器即可验证路由——执行结果是
    not_initialized，但说明 server 端确实查询的是 canonical。
    """
    workspace, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")
    (workspace / "tasks" / "rag" / "containers" / "sanva-personal").mkdir(parents=True)

    resp = client.post(
        "/search",
        headers=auth_headers(),
        json={"container": "sanva", "query": "foo", "topk": 3},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # response.container 保留客户端入参（无感原则）
    assert body["container"] == "sanva"
    # 但 server 实际查询的是 canonical
    assert "sanva-personal" in body["containers"]
    assert body["per_container_status"]["sanva-personal"] == "not_initialized"


def test_search_removed_alias_410(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="dava", canonical="sanva-yzjx", status="removed")
    resp = client.post(
        "/search",
        headers=auth_headers(),
        json={"container": "dava", "query": "foo"},
    )
    assert resp.status_code == 410


def test_search_containers_array_resolves_each(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")
    (workspace / "tasks" / "rag" / "containers" / "sanva-personal").mkdir(parents=True)
    (workspace / "tasks" / "rag" / "containers" / "sanva-yzjx").mkdir(parents=True)

    resp = client.post(
        "/search",
        headers=auth_headers(),
        json={"containers": ["sanva", "sanva-yzjx"], "query": "foo"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 两个目标都 resolve 完了
    assert set(body["containers"]) == {"sanva-personal", "sanva-yzjx"}


# ---- DELETE /containers/{name} 不 resolve alias —— 误删保护 -----------


def test_delete_container_via_alias_400(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    (workspace / "tasks" / "rag" / "containers" / "sanva-personal").mkdir(parents=True)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")

    resp = client.delete("/containers/sanva", headers=auth_headers())
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error"] == "cannot_delete_via_alias"
    assert detail["canonical"] == "sanva-personal"
    # canonical 数据未被破坏
    assert (workspace / "tasks" / "rag" / "containers" / "sanva-personal").exists()


def test_delete_container_canonical_still_works(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    (workspace / "tasks" / "rag" / "containers" / "sanva-personal").mkdir(parents=True)
    # 注册 alias，但 DELETE canonical 不受影响（只拦 alias 名调用）
    _seed_alias(client, alias="sanva", canonical="sanva-personal")

    resp = client.delete("/containers/sanva-personal", headers=auth_headers())
    assert resp.status_code == 200
    assert not (workspace / "tasks" / "rag" / "containers" / "sanva-personal").exists()


# ---- 索引状态 / backlog 透传 ------------------------------------------


def test_container_index_status_via_alias(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    (workspace / "tasks" / "rag" / "containers" / "sanva-personal").mkdir(parents=True)
    _seed_alias(client, alias="sanva", canonical="sanva-personal")

    resp = client.get(
        "/containers/sanva/index-status", headers=auth_headers()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # canonical 名出现在 container 字段（响应直接由 _compute_container_index_status 构造）
    assert body["container"] == "sanva-personal"
