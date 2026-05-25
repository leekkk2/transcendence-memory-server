"""Tests for container_metadata (Phase 1.2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import auth_headers, load_server, make_workspace


# ---- ContainerMetadata 单元 CRUD ------------------------------------------


def test_metadata_crud_basic(tmp_path: Path):
    from scripts.container_metadata import ContainerMetadata

    cm = ContainerMetadata(str(tmp_path / "lancedb"))
    assert cm.list_all() == []
    assert cm.get("foo") is None

    row = cm.upsert(
        "foo",
        description="hello",
        tags=["a", "b"],
        scope="team",
        entity="example-app",
        purpose="eng",
        owner="claude",
        policy={"retention_days": 365, "auto_reembed": True},
    )
    assert row["name"] == "foo"
    assert row["description"] == "hello"
    assert row["tags"] == ["a", "b"]
    assert row["scope"] == "team"
    assert row["entity"] == "example-app"
    assert row["purpose"] == "eng"
    assert row["owner"] == "claude"
    assert row["policy"] == {"retention_days": 365, "auto_reembed": True}
    assert row["created_at"]
    assert row["updated_at"]

    got = cm.get("foo")
    assert got is not None
    assert got["description"] == "hello"
    assert got["tags"] == ["a", "b"]


def test_metadata_partial_upsert_preserves_fields(tmp_path: Path):
    from scripts.container_metadata import ContainerMetadata

    cm = ContainerMetadata(str(tmp_path / "lancedb"))
    cm.upsert("foo", description="v1", tags=["a"], scope="team")
    cm.upsert("foo", description="v2")
    row = cm.get("foo")
    assert row["description"] == "v2"
    # tags / scope 不在第二次 upsert 中，保持原值
    assert row["tags"] == ["a"]
    assert row["scope"] == "team"


def test_metadata_list_all_sorted_and_delete(tmp_path: Path):
    from scripts.container_metadata import ContainerMetadata

    cm = ContainerMetadata(str(tmp_path / "lancedb"))
    cm.upsert("zeta", description="z")
    cm.upsert("alpha", description="a")
    cm.upsert("middle", description="m")
    names = [r["name"] for r in cm.list_all()]
    assert names == ["alpha", "middle", "zeta"]

    assert cm.delete("middle") is True
    assert cm.delete("middle") is False
    assert [r["name"] for r in cm.list_all()] == ["alpha", "zeta"]


def test_metadata_empty_name_rejected(tmp_path: Path):
    from scripts.container_metadata import ContainerMetadata

    cm = ContainerMetadata(str(tmp_path / "lancedb"))
    with pytest.raises(ValueError):
        cm.upsert("")
    assert cm.get("") is None
    assert cm.delete("") is False


# ---- HTTP endpoints --------------------------------------------------------


def _build_client(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    return workspace, client


def test_post_metadata_upsert_200(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    payload = {
        "description": "ExampleApp eng container",
        "tags": ["example-app", "engineering"],
        "scope": "team",
        "entity": "example-app",
        "purpose": "eng",
        "owner": "claude-on-mac",
        "policy": {"retention_days": 365, "auto_reembed": True},
    }
    resp = client.post(
        "/containers/example-app-eng/metadata",
        headers=auth_headers(),
        json=payload,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "example-app-eng"
    assert body["description"] == "ExampleApp eng container"
    assert body["tags"] == ["example-app", "engineering"]
    assert body["scope"] == "team"
    assert body["entity"] == "example-app"
    assert body["purpose"] == "eng"
    assert body["policy"] == {"retention_days": 365, "auto_reembed": True}
    assert body["created_at"]
    assert body["updated_at"]


def test_post_metadata_rejects_bad_name(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.post(
        "/containers/foo bar/metadata",
        headers=auth_headers(),
        json={"description": "x"},
    )
    assert resp.status_code in (400, 422)


def test_post_metadata_requires_auth(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.post(
        "/containers/foo/metadata",
        json={"description": "x"},
    )
    assert resp.status_code in (401, 403)


def test_get_containers_includes_metadata(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)

    # 准备两个容器：alpha 带 metadata，beta 不带
    root = workspace / "tasks" / "rag" / "containers"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir(parents=True)

    # 仅给 alpha 写 metadata
    resp = client.post(
        "/containers/alpha/metadata",
        headers=auth_headers(),
        json={
            "description": "alpha container",
            "tags": ["t1"],
            "scope": "shared",
            "entity": "alpha",
            "purpose": "active",
        },
    )
    assert resp.status_code == 200

    resp = client.get("/containers", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    names = {c["name"]: c for c in body["containers"]}
    assert set(names) == {"alpha", "beta"}

    # 旧字段保持向后兼容
    for c in body["containers"]:
        assert "objects" in c
        assert "indexed" in c
        assert "last_modified" in c
        assert "index_state" in c
        assert "metadata" in c  # 新字段始终存在（可能为 null）

    # alpha 有 metadata
    assert names["alpha"]["metadata"] is not None
    assert names["alpha"]["metadata"]["description"] == "alpha container"
    assert names["alpha"]["metadata"]["scope"] == "shared"
    assert names["alpha"]["metadata"]["tags"] == ["t1"]
    # beta 没有 metadata
    assert names["beta"]["metadata"] is None


def test_get_container_dump_returns_ndjson(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)

    # 准备容器与 3 条 memory_objects
    root = workspace / "tasks" / "rag" / "containers" / "demo"
    root.mkdir(parents=True)
    jsonl_path = root / "memory_objects.jsonl"
    rows = [
        {"id": "a", "text": "hello", "tags": ["x"]},
        {"id": "b", "text": "world", "metadata": {"k": "v"}},
        {"id": "c", "text": "中文 unicode", "tags": []},
    ]
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    resp = client.get("/containers/demo/dump", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    text = resp.text
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert len(lines) == 3
    parsed = [json.loads(ln) for ln in lines]
    ids = [r["id"] for r in parsed]
    assert ids == ["a", "b", "c"]
    # vector 列被剥离（即便源数据没有，也不能爆）
    assert all("vector" not in r for r in parsed)


def test_get_container_dump_empty_when_missing(tmp_path: Path, monkeypatch):
    _, client = _build_client(tmp_path, monkeypatch)
    resp = client.get("/containers/nonexistent/dump", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.text == ""


def test_get_container_dump_skips_corrupt_lines(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    root = workspace / "tasks" / "rag" / "containers" / "demo"
    root.mkdir(parents=True)
    jsonl_path = root / "memory_objects.jsonl"
    jsonl_path.write_text(
        '{"id":"good1"}\n'
        'not-valid-json\n'
        '{"id":"good2"}\n',
        encoding="utf-8",
    )
    resp = client.get("/containers/demo/dump", headers=auth_headers())
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.split("\n") if ln.strip()]
    assert len(lines) == 2
    ids = [json.loads(ln)["id"] for ln in lines]
    assert ids == ["good1", "good2"]


def test_get_container_dump_strips_vector_column(tmp_path: Path, monkeypatch):
    workspace, client = _build_client(tmp_path, monkeypatch)
    root = workspace / "tasks" / "rag" / "containers" / "demo"
    root.mkdir(parents=True)
    jsonl_path = root / "memory_objects.jsonl"
    jsonl_path.write_text(
        json.dumps({"id": "a", "text": "x", "vector": [0.1, 0.2, 0.3]}) + "\n",
        encoding="utf-8",
    )
    resp = client.get("/containers/demo/dump", headers=auth_headers())
    assert resp.status_code == 200
    line = next(ln for ln in resp.text.split("\n") if ln.strip())
    parsed = json.loads(line)
    assert parsed["id"] == "a"
    assert "vector" not in parsed
