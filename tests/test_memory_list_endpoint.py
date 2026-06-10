"""Tests for GET /containers/{container}/memories (paginated list) and the
search-failure humanizer that replaced bare "error: exit 1" statuses."""
from __future__ import annotations

from pathlib import Path

from conftest import auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


CONTAINER = "listtest"


def _seed_objects(client: TestClient, count: int) -> None:
    """顺序写入 count 个对象（ids obj-000..obj-N），storedAt 由 server 注入。"""
    objects = [
        {"id": f"obj-{i:03d}", "text": f"memory number {i}", "title": f"t{i}", "source": "pytest"}
        for i in range(count)
    ]
    resp = client.post(
        "/ingest-memory/objects",
        headers=auth_headers(),
        json={"container": CONTAINER, "objects": objects},
    )
    assert resp.status_code == 200, resp.text


def test_list_memories_default_returns_all(tmp_path: Path, monkeypatch):
    """不传 limit/offset → 全量返回（向后兼容默认），total 与 items 一致。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    _seed_objects(client, 5)

    resp = client.get(f"/containers/{CONTAINER}/memories", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["limit"] is None
    assert body["offset"] == 0
    assert len(body["items"]) == 5
    assert body["container"] == CONTAINER


def test_list_memories_pagination_slices_without_overlap(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    _seed_objects(client, 7)

    page1 = client.get(
        f"/containers/{CONTAINER}/memories?limit=3&offset=0", headers=auth_headers()
    ).json()
    page2 = client.get(
        f"/containers/{CONTAINER}/memories?limit=3&offset=3", headers=auth_headers()
    ).json()
    page3 = client.get(
        f"/containers/{CONTAINER}/memories?limit=3&offset=6", headers=auth_headers()
    ).json()

    assert page1["total"] == page2["total"] == page3["total"] == 7
    assert [len(p["items"]) for p in (page1, page2, page3)] == [3, 3, 1]
    ids = [it["id"] for p in (page1, page2, page3) for it in p["items"]]
    assert len(ids) == len(set(ids)) == 7  # 无重叠、无遗漏


def test_list_memories_offset_beyond_total(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    _seed_objects(client, 2)

    body = client.get(
        f"/containers/{CONTAINER}/memories?limit=50&offset=100", headers=auth_headers()
    ).json()
    assert body["total"] == 2
    assert body["items"] == []


def test_list_memories_limit_clamped(tmp_path: Path, monkeypatch):
    """limit 超界 clamp 到 1..500，offset 负值 clamp 到 0 —— 不 422。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    _seed_objects(client, 3)

    resp = client.get(
        f"/containers/{CONTAINER}/memories?limit=9999&offset=-5", headers=auth_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 500
    assert body["offset"] == 0
    assert len(body["items"]) == 3


def test_list_memories_empty_container(tmp_path: Path, monkeypatch):
    """容器不存在 / 无 JSONL → 空列表 + total=0（与浏览语义一致，不 404）。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    body = client.get("/containers/nosuch/memories", headers=auth_headers()).json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_memories_requires_auth(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get(f"/containers/{CONTAINER}/memories")
    assert resp.status_code in (401, 403)


# ---- _humanize_search_failure：搜索失败文案带原因，不再裸透传退出码 ----


def test_humanize_search_failure_connection(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)

    msg = server._humanize_search_failure(
        1, "httpx.ConnectError: [Errno 61] Connection refused", ""
    )
    assert msg == "error: embedding backend unreachable (exit 1)"


def test_humanize_search_failure_timeout_and_auth(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)

    assert "timed out" in server._humanize_search_failure(1, "ReadTimeout: timed out", "")
    assert "auth failed" in server._humanize_search_failure(
        1, "openai.AuthenticationError: Error code: 401 - invalid_api_key", ""
    )


def test_humanize_search_failure_model_fallback_wrapper(tmp_path: Path, monkeypatch):
    """真实栈实测：ConnectError 被 model_fallback 包装成 NoUpstreamAvailable，
    不含 'connection refused' 字样 —— 须由 wrapper 兜底分类，不落到裸 tail。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)

    stderr = (
        "model_fallback.NoUpstreamAvailable: all embed profiles failed "
        "(chain=[legacy], last_failed=legacy, last_error=ModelTransientError("
        "\"Embedding request failed after 3 retries: embedding upstream 502: ''\"))"
    )
    msg = server._humanize_search_failure(1, stderr, "")
    assert msg == "error: embedding backend unavailable (all profiles failed) (exit 1)"


def test_humanize_search_failure_unknown_keeps_stderr_tail(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)

    msg = server._humanize_search_failure(1, "Traceback ...\nValueError: weird failure", "")
    assert msg.startswith("error: exit 1 — ")
    assert "ValueError: weird failure" in msg
    # 完全无 stderr → 维持旧文案（向后兼容兜底）
    assert server._humanize_search_failure(1, "", "") == "error: exit 1"
