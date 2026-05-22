"""链路 5 —— VLM fallback chain 测试。

覆盖矩阵：
- raganything_engine.make_vision_model_func：primary VLM 429 → fallback 接管
- make_vision_model_func：全链挂掉 → NoUpstreamAvailable
- task_rag_server._resolve_caption_vlm_chain：按 route 展开 VLM 主+备链
- /embed-multimodal caption：链式生成成功（caption_source=vlm）
- /embed-multimodal caption：全链挂掉 → caption=None（best-effort 不阻塞落库）

HTTP 全程 mock —— VLM chat 走 httpx.AsyncClient（chat/completions），
gemini 多模态 / caption 走 httpx.AsyncClient（:embedContent / :generateContent）。
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.raganything_engine as rag_any  # noqa: E402
from scripts.model_fallback import NoUpstreamAvailable  # noqa: E402
from scripts.profiles_loader import VLMProfile  # noqa: E402

API_KEY = "test-rag-key"
_CHAT_URL = "/chat/completions"


# =========================================================================
# Part 1 —— make_vision_model_func 链式 fallback（chat/completions 层）
# =========================================================================


def _vlm(name: str) -> VLMProfile:
    """构造 VLMProfile，base_url 以 name 为前缀便于 mock 按 url 路由。"""
    return VLMProfile(
        name=name,
        model="test-vlm",
        base_url=f"https://{name}.example/v1",
        api_key="test-key",
        timeout_s=10.0,
        max_retries=1,
    )


class _ChatResp:
    """httpx.Response 兼容子集 —— 覆盖 call_openai_chat 走的接口。"""

    def __init__(self, status_code: int, content: str | None = None) -> None:
        self.status_code = status_code
        self._content = content
        self.text = content or f"err-{status_code}"
        self.request = httpx.Request("POST", "https://example.com/v1/chat/completions")

    def json(self) -> Any:
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )


class _FakeChatClient:
    """按 url 决定响应队列的假 httpx 客户端（chat/completions）。"""

    by_url: dict[str, list[_ChatResp]] = {}
    calls: list[str] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeChatClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _ChatResp:
        type(self).calls.append(url)
        queue = type(self).by_url.get(url)
        if not queue:
            raise AssertionError(f"unexpected post to {url!r} (no mock prepared)")
        return queue.pop(0)


def _install_chat(monkeypatch, by_url: dict[str, list[_ChatResp]]) -> list[str]:
    _FakeChatClient.by_url = by_url
    _FakeChatClient.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeChatClient)
    # 单 profile 内只试一次。call_openai_chat 由 raganything_engine 从某个
    # rag_engine 副本 import —— worker/server 双重 import 下可能不是
    # scripts.rag_engine 这一份；直接改它 __globals__ 里的常量，避免改错副本。
    monkeypatch.setitem(
        rag_any.call_openai_chat.__globals__, "_LLM_MAX_RETRIES", 1,
    )
    return _FakeChatClient.calls


def test_vision_func_fallback_on_primary_429(monkeypatch):
    """primary VLM 一次 429 → fallback 接管，返回 fallback 的 content。"""
    chain = [_vlm("primary-vlm"), _vlm("backup-vlm")]
    func = rag_any.make_vision_model_func(chain)
    calls = _install_chat(monkeypatch, {
        f"https://primary-vlm.example/v1{_CHAT_URL}": [_ChatResp(429)],
        f"https://backup-vlm.example/v1{_CHAT_URL}": [
            _ChatResp(200, content="caption from backup"),
        ],
    })

    out = asyncio.run(func(prompt="describe this image"))
    assert out == "caption from backup"
    assert len(calls) == 2
    assert calls[0].startswith("https://primary-vlm")
    assert calls[1].startswith("https://backup-vlm")


def test_vision_func_all_profiles_fail_raises_no_upstream(monkeypatch):
    """primary + backup VLM 都 5xx → NoUpstreamAvailable，错误信息含整条链。"""
    chain = [_vlm("primary-vlm"), _vlm("backup-vlm")]
    func = rag_any.make_vision_model_func(chain)
    _install_chat(monkeypatch, {
        f"https://primary-vlm.example/v1{_CHAT_URL}": [_ChatResp(503)],
        f"https://backup-vlm.example/v1{_CHAT_URL}": [_ChatResp(503)],
    })

    with pytest.raises(NoUpstreamAvailable) as ei:
        asyncio.run(func(prompt="describe"))
    assert "primary-vlm" in str(ei.value)
    assert "backup-vlm" in str(ei.value)


# =========================================================================
# Part 2 —— /embed-multimodal caption 链式 + 全链挂掉降级
# =========================================================================


_GEMINI_EMBED_YAML = """
version: 2
embeddings:
  - name: gem
    provider: gemini_native
    model: gemini-embedding-2
    dim: 8
    base_url: https://relay.example
    api_key_env: EMBEDDING_API_KEY
    max_retries: 1
    timeout_s: 30
routes:
  - match: {default: true}
    embedding: gem
"""

_GEMINI_VLM_YAML = """
version: 2
embeddings:
  - name: gem
    provider: gemini_native
    model: gemini-embedding-2
    dim: 8
    base_url: https://relay.example
    api_key_env: EMBEDDING_API_KEY
vlms:
  - name: vlm-a
    model: vision-a
    base_url: https://vlm-a.example
    api_key_env: EMBEDDING_API_KEY
  - name: vlm-b
    model: vision-b
    base_url: https://vlm-b.example
    api_key_env: EMBEDDING_API_KEY
routes:
  - match: {default: true}
    embedding: gem
    vlm: vlm-a
    vlm_fallbacks: [vlm-b]
"""


def _load_mm_server(tmp_path, monkeypatch, yaml_text: str):
    """重载 task_rag_server，挂上指定 profiles.yaml（gemini_native embedding）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(REPO_ROOT / "scripts")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "fake-key")
    yaml_path = workspace / "profiles.yaml"
    yaml_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("TM_PROFILES_FILE", str(yaml_path))

    for mod in list(sys.modules):
        if mod.startswith("scripts.task_rag_server") or mod == "task_rag_server":
            sys.modules.pop(mod, None)
    for mod_name in ("scripts.embedding_registry", "embedding_registry"):
        if mod_name in sys.modules:
            try:
                sys.modules[mod_name].clear_registry()
            except Exception:  # pragma: no cover
                pass
    return importlib.import_module("scripts.task_rag_server")


class _GemResp:
    """gemini 原生响应兼容子集（embedContent / generateContent）。"""

    def __init__(self, status: int, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = payload
        self.text = "" if payload else f"err-{status}"
        self.request = httpx.Request("POST", "https://relay.example")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )


class _FakeGeminiClient:
    """假 gemini relay：embedContent 恒成功；generateContent 由 caption_ok 决定。"""

    caption_ok = True

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeGeminiClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _GemResp:
        if ":embedContent" in url:
            return _GemResp(200, {"embedding": {"values": [0.1] * 8}})
        if ":generateContent" in url:
            if type(self).caption_ok:
                return _GemResp(200, {
                    "candidates": [
                        {"content": {"parts": [{"text": "a red square"}]}},
                    ],
                })
            return _GemResp(503)
        raise AssertionError(f"unexpected gemini url {url!r}")


def test_resolve_caption_vlm_chain_expands_route(tmp_path, monkeypatch):
    """route 配置 vlm + vlm_fallbacks → caption VLM 链按序展开主+备。"""
    server = _load_mm_server(tmp_path, monkeypatch, _GEMINI_VLM_YAML)
    # route.vlm 已配置时 embed_profile 参数不参与 —— 传 None 即可。
    chain = server._resolve_caption_vlm_chain("anybox", None)
    assert [p.name for p in chain] == ["vlm-a", "vlm-b"]
    assert [p.model for p in chain] == ["vision-a", "vision-b"]


def test_embed_multimodal_caption_generated(tmp_path, monkeypatch):
    """/embed-multimodal：caption 链式生成成功 → caption_source=vlm。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    server = _load_mm_server(tmp_path, monkeypatch, _GEMINI_EMBED_YAML)
    _FakeGeminiClient.caption_ok = True
    monkeypatch.setattr(httpx, "AsyncClient", _FakeGeminiClient)
    client = TestClient(server.app)

    resp = client.post(
        "/embed-multimodal",
        headers={"X-API-KEY": API_KEY},
        data={"container": "mmbox"},
        files={"file": ("pic.png", b"fake-png-bytes", "image/png")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["caption_source"] == "vlm"
    assert body["caption"] == "a red square"
    # caption 行也落库 —— media 行 + caption 行
    assert body["caption_chunk_id"]


def test_embed_multimodal_caption_chain_exhausted_degrades_to_none(
    tmp_path, monkeypatch,
):
    """/embed-multimodal：caption VLM 链全挂 → caption=None，媒体行仍正常落库。"""
    pytest.importorskip("lancedb")
    from fastapi.testclient import TestClient

    server = _load_mm_server(tmp_path, monkeypatch, _GEMINI_EMBED_YAML)
    _FakeGeminiClient.caption_ok = False  # generateContent 持续 503
    monkeypatch.setattr(httpx, "AsyncClient", _FakeGeminiClient)
    client = TestClient(server.app)

    resp = client.post(
        "/embed-multimodal",
        headers={"X-API-KEY": API_KEY},
        data={"container": "mmbox"},
        files={"file": ("pic.png", b"fake-png-bytes", "image/png")},
    )
    # caption 全挂是 best-effort —— 不阻塞媒体行落库
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["caption_source"] == "none"
    assert body["caption"] == ""
    assert body["caption_chunk_id"] is None
