"""Tests for memory CRUD endpoints (PUT/DELETE on memories)."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import API_KEY, auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


CONTAINER = "crudtest"


def _seed_object(client: TestClient, obj_id: str = "obj-001", text: str = "hello world") -> None:
    """在 container 中写入一个测试对象。"""
    client.post(
        "/ingest-memory/objects",
        headers=auth_headers(),
        json={
            "container": CONTAINER,
            "objects": [{"id": obj_id, "text": text, "title": "test", "source": "pytest"}],
        },
    )


def _read_jsonl(workspace: Path) -> list[dict]:
    path = workspace / "tasks" / "rag" / "containers" / CONTAINER / "memory_objects.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_update_memory(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    _seed_object(client, "obj-001", "original text")

    resp = client.put(
        f"/containers/{CONTAINER}/memories/obj-001",
        headers=auth_headers(),
        json={"text": "updated text", "tags": ["new-tag"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated"] is True
    assert body["id"] == "obj-001"

    rows = _read_jsonl(workspace)
    assert len(rows) == 1
    assert rows[0]["text"] == "updated text"
    assert rows[0]["tags"] == ["new-tag"]
    assert "updatedAt" in rows[0]


def test_update_memory_not_found(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # container 目录需要存在但 JSONL 为空
    (workspace / "tasks" / "rag" / "containers" / CONTAINER).mkdir(parents=True)

    resp = client.put(
        f"/containers/{CONTAINER}/memories/nonexistent",
        headers=auth_headers(),
        json={"text": "nope"},
    )
    assert resp.status_code == 404


def test_delete_memory(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    _seed_object(client, "obj-del", "to be deleted")

    resp = client.delete(
        f"/containers/{CONTAINER}/memories/obj-del",
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True

    rows = _read_jsonl(workspace)
    assert all(r["id"] != "obj-del" for r in rows)


def test_delete_memory_not_found(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    (workspace / "tasks" / "rag" / "containers" / CONTAINER).mkdir(parents=True)

    resp = client.delete(
        f"/containers/{CONTAINER}/memories/ghost",
        headers=auth_headers(),
    )
    assert resp.status_code == 404
