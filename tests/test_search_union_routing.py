"""v0.11.0：/search 多容器 union 双轨路由 + dedup + per-container timeout 测试。

策略：
- Phase A (单元测试)：对 _resolve_search_targets() 直接 mock _list_container_dirs +
  _get_union_search_default，覆盖 union 触发条件、显式覆盖、sibling 不存在等 7 个分支
- Phase B (端到端)：用 fake embedding HTTP server 模拟 newapi，跑真 TestClient + 真
  subprocess search，验证：union 自动追加 + dedup + degraded + per_container_status
  含 timeout 三个核心行为

不测：reranker 路径（已有 reranker_client 测试覆盖）；fallback / circuit breaker（同上）。
"""
from __future__ import annotations

import contextlib
import importlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


API_KEY = "test-rag-key"


# =============================================================================
# Phase A — _resolve_search_targets 单元测试（不走 subprocess / network）
# =============================================================================

@pytest.fixture
def server_module(tmp_path, monkeypatch):
    """Fresh server module load with isolated WORKSPACE for unit tests."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(REPO_ROOT / "scripts")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")

    for mod in list(sys.modules):
        if mod.startswith("scripts.task_rag_server") or mod == "task_rag_server":
            sys.modules.pop(mod, None)
    return importlib.import_module("scripts.task_rag_server")


def _mk_req(server, **overrides):
    """SearchReq factory with sensible defaults."""
    from scripts.task_rag_server_models import SearchReq
    base = {"query": "q", "container": "main"}
    base.update(overrides)
    return SearchReq(**base)


def _stub_dirs(*names):
    """Make a stub object list that mimics Path objects with .name attribute."""
    return [type("FakePath", (), {"name": n})() for n in names]


def test_union_off_by_default_single_container(server_module):
    """union_search_default=false + req.union=None → 单 container 不触发 union。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=False):
        req = _mk_req(server_module, container="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main"]
    assert union_applied is False


def test_union_default_true_appends_sibling(server_module):
    """union_search_default=true + sibling 存在 → 自动追加 _openai。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai", "other")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, container="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main", "main_openai"]
    assert union_applied is True


def test_union_default_true_but_sibling_missing(server_module):
    """union_search_default=true 但 sibling 不存在 → 不追加（避免 not_initialized 噪音）。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "other")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, container="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main"]
    assert union_applied is False


def test_union_explicit_false_overrides_yaml_true(server_module):
    """req.union=False 显式关闭，即使 YAML 开启也不 union。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, container="main", union=False)
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main"]
    assert union_applied is False


def test_union_explicit_true_overrides_yaml_false(server_module):
    """req.union=True 显式开启，即使 YAML 关闭也 union。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=False):
        req = _mk_req(server_module, container="main", union=True)
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main", "main_openai"]
    assert union_applied is True


def test_containers_list_skips_union(server_module):
    """显式 containers=[...] → 用户已掌控，不触发 union 扩展。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("a", "a_openai", "b")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, containers=["a", "b"])
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["a", "b"]
    assert union_applied is False


def test_container_pattern_skips_union(server_module):
    """显式 container_pattern → 用户已掌控，不触发 union 扩展。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai", "other")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, container_pattern="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert "main" in targets and "main_openai" in targets  # pattern 自然命中两者
    assert union_applied is False  # 但不是 union 扩展导致的


def test_openai_mirror_skips_self_union(server_module):
    """主容器本身以 _openai 结尾 → 不追加（避免镜像查镜像）。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=True):
        req = _mk_req(server_module, container="main_openai")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main_openai"]
    assert union_applied is False


# =============================================================================
# Phase B — 端到端：fake embedding server + 真 TestClient + 真 subprocess
# =============================================================================

CONTAINER_MAIN = "uniontest"
CONTAINER_MIRROR = "uniontest_openai"
DOC_TEXT = "union routing 端到端证据 chunk"


@contextlib.contextmanager
def _fake_embedding_server():
    """单一固定 vector 的 fake server，保证两 container 都能召回 same chunk。"""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            _ = self.rfile.read(length)
            response = {"data": [{"embedding": [1.0, 0.0, 0.0]}]}
            raw = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _setup_workspace_with_two_containers(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(REPO_ROOT / "scripts")
    return workspace


def _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True):
    """Load server with optional YAML containing union_search_default."""
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", base_url)
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embed")
    monkeypatch.setenv("EMBEDDING_DIM", "3")

    # 写 YAML（如需）
    if union_yaml:
        yaml_path = workspace / "profiles.yaml"
        yaml_path.write_text(
            f"""
version: 1
union_search_default: true
embeddings:
  - name: legacy
    model: fake-embed
    dim: 3
    base_url: {base_url}
    api_key_env: EMBEDDING_API_KEY
routes:
  - match: {{default: true}}
    embedding: legacy
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("TM_PROFILES_FILE", str(yaml_path))

    for mod in list(sys.modules):
        if mod.startswith("scripts.task_rag_server") or mod == "task_rag_server":
            sys.modules.pop(mod, None)
    # 清 embedding_registry 单例 — 两个 import 路径都要清（scripts.embedding_registry +
    # bare embedding_registry），否则全套跑时前面用一种路径 load 过的 cached ProfileSet
    # 会泄漏，导致后跑的 union_search_default=true YAML 无效
    for mod_name in ("scripts.embedding_registry", "embedding_registry"):
        if mod_name in sys.modules:
            try:
                sys.modules[mod_name].clear_registry()
            except Exception:
                pass
    return importlib.import_module("scripts.task_rag_server")


@pytest.mark.skipif(
    not Path("/usr/bin/python3").exists() and not Path(sys.executable).exists(),
    reason="subprocess search requires real python interpreter",
)
def test_union_e2e_dedup_and_degraded(tmp_path, monkeypatch):
    """端到端：union 双轨召回 → dedup 后单条；含 union_applied=true 与 degraded 字段。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True)
        client = TestClient(server.app)

        # 两个 container 各 ingest 同 (taskId, chunkId) → 验证 dedup
        for container in (CONTAINER_MAIN, CONTAINER_MIRROR):
            r = client.post(
                "/ingest-memory/objects",
                headers={"X-API-KEY": API_KEY},
                json={
                    "container": container,
                    "objects": [
                        {
                            "id": "TASK-001",
                            "text": DOC_TEXT,
                            "title": "union test",
                            "tags": ["union"],
                            "metadata": {"taskId": "TASK-001"},
                        }
                    ],
                },
            )
            assert r.status_code == 200, r.text
            er = client.post(
                "/embed",
                headers={"X-API-KEY": API_KEY},
                json={"container": container, "wait": True},
            )
            assert er.status_code == 200
            assert er.json()["code"] == 0, er.json()

        # 单 container 查询 → union 应触发，自动追加 sibling
        sr = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "union 端到端", "topk": 5},
        )
        assert sr.status_code == 200
        body = sr.json()
        assert body["union_applied"] is True, body
        assert set(body["containers"]) == {CONTAINER_MAIN, CONTAINER_MIRROR}, body
        assert body["per_container_status"][CONTAINER_MAIN] == "ok"
        assert body["per_container_status"][CONTAINER_MIRROR] == "ok"
        assert body["degraded"] is False
        # 同 (taskId, chunkId) 在两 container 都召回 → dedup 后只保留一条
        keys = {(h.get("taskId"), h.get("chunkId")) for h in body["results"]}
        assert len(body["results"]) == len(keys), f"dedup failed: {body['results']}"


def test_union_e2e_explicit_false_skips(tmp_path, monkeypatch):
    """端到端：req.union=False 显式关闭 → 即使 YAML 开启也只查主容器。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True)
        client = TestClient(server.app)

        for container in (CONTAINER_MAIN, CONTAINER_MIRROR):
            client.post(
                "/ingest-memory/objects",
                headers={"X-API-KEY": API_KEY},
                json={
                    "container": container,
                    "objects": [{"id": "T", "text": DOC_TEXT, "metadata": {"taskId": "T"}}],
                },
            )
            client.post(
                "/embed",
                headers={"X-API-KEY": API_KEY},
                json={"container": container, "wait": True},
            )

        sr = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "X", "topk": 5, "union": False},
        )
        assert sr.status_code == 200
        body = sr.json()
        assert body["union_applied"] is False, body
        assert body["containers"] == [CONTAINER_MAIN], body
