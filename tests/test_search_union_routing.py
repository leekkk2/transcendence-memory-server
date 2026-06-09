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
    """union_search_default=true + sibling 存在且 chunks 表就绪 → 自动追加 _openai。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai", "other")), \
         patch.object(server_module, "_get_union_search_default", return_value=True), \
         patch.object(server_module, "_container_has_chunks_table", return_value=True):
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
    """req.union=True 显式开启，即使 YAML 关闭也 union（sibling chunks 表就绪）。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=False), \
         patch.object(server_module, "_container_has_chunks_table", return_value=True):
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


@contextlib.contextmanager
def _fake_embedding_server_content_aware():
    """Content-aware fake server：vector 由请求文本派生 → query 与 doc 文本不同时
    必产生非零 L2 距离，供 score-gate（距离上界）测试。dim=3，第一维由文本长度+
    首字符决定，足以区分不同文本。"""
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body or b"{}")
            except Exception:
                payload = {}
            text = payload.get("input")
            if isinstance(text, list):
                text = text[0] if text else ""
            text = str(text or "")
            # 简单确定性派生：不同文本 → 不同 vector → 非零距离
            seed = (sum(ord(c) for c in text) % 97) / 10.0
            vec = [1.0 + seed, float(len(text) % 5), 0.5]
            response = {"data": [{"embedding": vec}]}
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


# =============================================================================
# P1 — _collapse_media_caption_hits：媒体行 / caption 兄弟行折叠
# =============================================================================

def _mk_hit(server, chunk_id, *, doc_type=None, score=None, parent=None):
    """SearchHit factory for caption-collapse unit tests."""
    from scripts.task_rag_server_models import SearchHit
    meta = {"parent_chunk_id": parent} if parent else {}
    return SearchHit(chunkId=chunk_id, taskId="T", docType=doc_type,
                     score=score, metadata=meta)


def test_collapse_caption_folds_into_parent(server_module):
    """媒体行 + caption 行同时命中 → caption 折叠，媒体行取较优分数。"""
    media = _mk_hit(server_module, "T#multimodal", doc_type="multimodal", score=0.40)
    cap = _mk_hit(server_module, "T#caption", doc_type="media_caption",
                  score=0.18, parent="T#multimodal")
    out = server_module._collapse_media_caption_hits([cap, media])
    assert len(out) == 1
    assert out[0].chunkId == "T#multimodal"
    assert out[0].score == 0.18  # 取较优（更小 distance）
    assert out[0].metadata.get("caption_hit") is True


def test_collapse_caption_kept_when_parent_absent(server_module):
    """父媒体行未命中 → caption 行原样保留。"""
    cap = _mk_hit(server_module, "T#caption", doc_type="media_caption",
                  score=0.2, parent="T#multimodal")
    out = server_module._collapse_media_caption_hits([cap])
    assert len(out) == 1
    assert out[0].chunkId == "T#caption"


def test_collapse_caption_keeps_better_media_score(server_module):
    """媒体行分数已优于 caption → 不下调。"""
    media = _mk_hit(server_module, "T#multimodal", doc_type="multimodal", score=0.10)
    cap = _mk_hit(server_module, "T#caption", doc_type="media_caption",
                  score=0.50, parent="T#multimodal")
    out = server_module._collapse_media_caption_hits([media, cap])
    assert len(out) == 1 and out[0].score == 0.10


# =============================================================================
# Phase 1 (2026-06-09) — 优雅降级根治 + not_initialized 软跳过 + score-gate
# =============================================================================


# --- 项2 · not_initialized sibling 软跳过（_resolve_search_targets 单元） -------

def test_union_skips_uninitialized_sibling(server_module):
    """sibling 目录存在但无 chunks 表 → 软跳过，不追加（避免 not_initialized 噪音）。

    项2 根治：未初始化镜像被 _resolve_search_targets 在解析阶段就剔除，
    返回 targets==[main]、union_applied==False。
    """
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=True), \
         patch.object(server_module, "_container_has_chunks_table", return_value=False):
        req = _mk_req(server_module, container="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main"]
    assert union_applied is False


def test_union_appends_initialized_sibling(server_module):
    """sibling 存在且 chunks 表就绪 → 正常追加（双轨恢复）。"""
    with patch.object(server_module, "_list_container_dirs", return_value=_stub_dirs("main", "main_openai")), \
         patch.object(server_module, "_get_union_search_default", return_value=True), \
         patch.object(server_module, "_container_has_chunks_table", return_value=True):
        req = _mk_req(server_module, container="main")
        targets, union_applied = server_module._resolve_search_targets(req)
    assert targets == ["main", "main_openai"]
    assert union_applied is True


def test_container_has_chunks_table_swallows_errors(server_module):
    """_container_has_chunks_table 任何异常吞为 False（只连不 embedding）。"""
    # 不存在的目录 → lancedb.connect 后 table_names 不含 'chunks'（或 connect 抛）→ False
    assert server_module._container_has_chunks_table("nonexistent-container-xyz") is False


# --- 项3 · per-container timeout 自适应 ----------------------------------------

def test_per_container_timeout_helper_default(server_module):
    """_get_union_per_container_timeout 异常回退 _DEFAULT_PER_CONTAINER_TIMEOUT_S。"""
    # 无 YAML 的 unit fixture → registry.profiles 无 union_per_container_timeout_s 自定义，
    # legacy ProfileSet 默认 30.0
    val = server_module._get_union_per_container_timeout()
    assert val == 30.0


# --- 项1 · 降级元数据 + 项4 · score-gate（端到端） ----------------------------

def _ingest_and_embed(client, container, *, task_id="TASK-001", text=DOC_TEXT):
    r = client.post(
        "/ingest-memory/objects",
        headers={"X-API-KEY": API_KEY},
        json={
            "container": container,
            "objects": [
                {
                    "id": task_id,
                    "text": text,
                    "title": "degrade test",
                    "tags": ["degrade"],
                    "metadata": {"taskId": task_id},
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


def test_e2e_soft_skip_uninitialized_sibling(tmp_path, monkeypatch):
    """项2 端到端：主容器 ok + sibling 目录存在但未 embed（无 chunks 表）+ union →
    sibling 软跳过 → containers==[main]、union_applied==False、无 not_initialized 噪音。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True)
        client = TestClient(server.app)

        # 仅主容器 ingest+embed；sibling 只 ingest（建目录）但不 embed → 无 chunks 表
        _ingest_and_embed(client, CONTAINER_MAIN)
        ir = client.post(
            "/ingest-memory/objects",
            headers={"X-API-KEY": API_KEY},
            json={
                "container": CONTAINER_MIRROR,
                "objects": [{"id": "T", "text": DOC_TEXT, "metadata": {"taskId": "T"}}],
            },
        )
        assert ir.status_code == 200, ir.text

        sr = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "degrade 软跳过", "topk": 5},
        )
        assert sr.status_code == 200
        body = sr.json()
        assert body["containers"] == [CONTAINER_MAIN], body
        assert body["union_applied"] is False, body
        assert "not_initialized" not in body["per_container_status"].values(), body
        assert body["status"] == "ok", body
        assert body["results"], body


def test_e2e_degraded_partial_containers(tmp_path, monkeypatch):
    """项1 端到端：主 ok + 第三容器失败 → status='ok'、is_degraded==True、
    fallback_source=='partial_containers'、results 非空、is_degraded==degraded、HTTP 200。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True)
        client = TestClient(server.app)

        _ingest_and_embed(client, CONTAINER_MAIN)
        # 显式 containers=[main, ghost]：ghost 不存在 → 失败 / not_initialized
        sr = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={
                "containers": [CONTAINER_MAIN, "ghostcontainer"],
                "query": "degrade partial",
                "topk": 5,
            },
        )
        assert sr.status_code == 200
        body = sr.json()
        assert body["status"] == "ok", body
        assert body["is_degraded"] is True, body
        assert body["is_degraded"] == body["degraded"], body
        assert body["fallback_source"] == "partial_containers", body
        assert body["results"], body
        # 部分成功不弹红条
        assert body["message"] is None, body


def test_e2e_all_failed_is_error(tmp_path, monkeypatch):
    """项1 端到端：全失败 → status='error'、fallback_source 为 None、message 有人话。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=True)
        client = TestClient(server.app)

        sr = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={
                "containers": ["ghost-a", "ghost-b"],
                "query": "all failed",
                "topk": 5,
            },
        )
        assert sr.status_code == 200
        body = sr.json()
        assert body["status"] == "error", body
        assert body["is_degraded"] is True, body
        assert body["fallback_source"] is None, body
        assert body["message"], body


def test_e2e_score_gate_blocks_low_score(tmp_path, monkeypatch):
    """项4 端到端：score_threshold 极小距离上界 → blocked_low_score>0、results 被拦。

    content-aware fake embedding：query 与 doc 文本不同 → 必有非零 L2 距离 →
    极小正阈值（0.001）拦截全部。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server_content_aware() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=False)
        client = TestClient(server.app)

        _ingest_and_embed(client, CONTAINER_MAIN)

        # baseline：无阈值 → 有结果
        base = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "score gate distinct query", "topk": 5},
        ).json()
        assert base["results"], base
        assert base.get("blocked_low_score", 0) == 0, base

        # 极小正距离上界 → 全拦（doc 与 query 距离 >> 0.001）
        gated = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={
                "container": CONTAINER_MAIN,
                "query": "score gate distinct query",
                "topk": 5,
                "score_threshold": 0.001,
            },
        ).json()
        assert gated["blocked_low_score"] > 0, gated
        assert gated["results"] == [], gated


def test_e2e_score_gate_none_unchanged(tmp_path, monkeypatch):
    """项4 端到端：score_threshold=None → 行为不变（与 baseline 一致），blocked_low_score==0。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=False)
        client = TestClient(server.app)

        _ingest_and_embed(client, CONTAINER_MAIN)
        body = client.post(
            "/search",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "no gate", "topk": 5, "score_threshold": None},
        ).json()
        assert body["results"], body
        assert body["blocked_low_score"] == 0, body
        # citation_enabled 默认开 → citations 投影非空
        assert body["citations"], body


def test_e2e_query_score_gated_skips_llm(tmp_path, monkeypatch):
    """项4 端到端：/query score_threshold 生效且超阈值 → status='score_gated' 不调 LLM。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server_content_aware() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=False)
        client = TestClient(server.app)

        _ingest_and_embed(client, CONTAINER_MAIN)
        r = client.post(
            "/query",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "gated distinct query", "score_threshold": 0.001},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "score_gated", body
        assert body["answer"] == "", body
        assert body.get("top_score") is not None, body


# =============================================================================
# Adversarial review remediation (2026-06-09) — #5 / #2b 回归
# =============================================================================


# --- #5 · rerank-only 失败不得误标 fallback_source=partial_containers -----------

def test_rerank_only_failure_degraded_but_no_partial_fallback(server_module, monkeypatch):
    """#5 回归：容器全 ok 但 rerank 抛异常（rerank_warning 非 None → degraded=True）→
    is_degraded==True 但 fallback_source 为 None（**不是** 'partial_containers'）。

    容器层无 partial（per_container_status 全 'ok'），降级纯由 rerank 失败导致 →
    fallback_source 必须为 None，rerank 降级信息靠 is_degraded + message 表达。
    """
    from scripts.task_rag_server_models import SearchReq

    async def _raising_rerank(_query, _docs, top_n=None, **_kwargs):
        raise RuntimeError("upstream reranker 503")

    def fake_resolve_targets(_req):
        return ["main"], False

    def fake_resolve_rerank(_req, targets):
        # rerank 启用，返回会抛异常的 rerank_func → 触发 rerank_warning 路径
        return True, _raising_rerank, "fake-reranker", 30

    def fake_run_single_search(query, topk, container, timeout_s, embedding_override=None):
        payload = {
            "code": "ok",
            "results": [
                {"text": "alpha", "score": 0.10, "taskId": "t1", "chunkId": "a"},
                {"text": "beta", "score": 0.20, "taskId": "t2", "chunkId": "b"},
            ],
        }
        return server_module.CommandResponse(command=["fake"], code=0), payload

    monkeypatch.setattr(server_module, "_resolve_search_targets", fake_resolve_targets)
    monkeypatch.setattr(server_module, "_resolve_search_rerank", fake_resolve_rerank)
    monkeypatch.setattr(server_module, "_run_single_search", fake_run_single_search)

    body = server_module.search(SearchReq(query="q", container="main", topk=2))

    # 容器全 ok → status ok、有结果；但 rerank 失败 → degraded
    assert body.status == "ok", body
    assert body.results, body
    assert body.rerank_applied is False, body
    assert body.is_degraded is True, body
    assert body.is_degraded == body.degraded, body
    # 核心断言：rerank-only 失败 → fallback_source 必须为 None（非 partial_containers）
    assert body.fallback_source is None, body
    # per_container_status 全 ok（容器层无 partial）
    assert all(s == "ok" for s in body.per_container_status.values()), body


# --- #2b · /query gate 区分 not_initialized vs score_gated ---------------------

def test_e2e_query_not_initialized_not_score_gated(tmp_path, monkeypatch):
    """#2b 回归：/query 开启 score_threshold 但目标容器未初始化（无 chunks 表）→
    返回 status=='not_initialized'（**非** 'score_gated'），让 Agent 知道该先 /embed。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server_content_aware() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=False)
        client = TestClient(server.app)

        # 仅 ingest 建目录，不 embed → 无 chunks 表（container_not_initialized）
        ir = client.post(
            "/ingest-memory/objects",
            headers={"X-API-KEY": API_KEY},
            json={
                "container": CONTAINER_MAIN,
                "objects": [{"id": "T", "text": DOC_TEXT, "metadata": {"taskId": "T"}}],
            },
        )
        assert ir.status_code == 200, ir.text

        r = client.post(
            "/query",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "uninitialized query", "score_threshold": 0.001},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "not_initialized", body
        assert body["status"] != "score_gated", body
        assert body["answer"] == "", body
        assert body.get("top_score") is None, body


def test_e2e_query_initialized_over_threshold_still_score_gated(tmp_path, monkeypatch):
    """#2b 回归（守住正常 gate）：容器 ok 且 top1 距离 > 阈值 → 仍 status=='score_gated'。
    确认修复区分 not_initialized 后，正常的「分数太低」gate 未被破坏。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    workspace = _setup_workspace_with_two_containers(tmp_path)
    with _fake_embedding_server_content_aware() as base_url:
        server = _load_e2e_server(workspace, monkeypatch, base_url, union_yaml=False)
        client = TestClient(server.app)

        _ingest_and_embed(client, CONTAINER_MAIN)
        r = client.post(
            "/query",
            headers={"X-API-KEY": API_KEY},
            json={"container": CONTAINER_MAIN, "query": "distinct over threshold query", "score_threshold": 0.001},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "score_gated", body
        assert body["status"] != "not_initialized", body
        assert body["answer"] == "", body
        assert body.get("top_score") is not None, body
