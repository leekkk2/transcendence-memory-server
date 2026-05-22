"""reranker_registry 单元测试：HTTP schema 解析、min_score 过滤、429 重试、
empty-doc 早返、Cohere v2 兼容。

与 test_embedding_registry.py 风格一致 — 用 monkeypatch + 假 httpx 客户端
拦截真实网络，验证 build_rerank_func 的端到端行为契约。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.profiles_loader import RerankerProfile  # noqa: E402
from scripts.reranker_registry import (  # noqa: E402
    RerankerRegistry,
    clear_reranker_registry,
    get_reranker_registry,
)
import scripts.reranker_registry as rrk_mod  # noqa: E402


# ---- helpers ------------------------------------------------------------


def _make_profile(
    name: str = "selfhosted-bge",
    base_url: str = "https://newapi.example/v1",
    min_score: float = 0.0,
    api_key: str = "test-key",
    timeout_s: float = 30.0,
) -> RerankerProfile:
    return RerankerProfile(
        name=name,
        provider="cohere_compatible",
        model="text-reranker",
        base_url=base_url,
        api_key=api_key,
        timeout_s=timeout_s,
        min_score=min_score,
    )


def _make_registry() -> RerankerRegistry:
    """空 ProfileSet 也能构造 registry — 测试不依赖 get_profile 走全套加载。"""
    from scripts.profiles_loader import ProfileSet, Route
    ps = ProfileSet(
        embeddings={},  # type: ignore[arg-type]
        rerankers={"selfhosted-bge": _make_profile()},
        routes=[],
        default_route=Route(embedding="anything"),  # 仅占位，不会被本测试 resolve
    )
    return RerankerRegistry(ps)


class _MockResponse:
    """最小 httpx.Response 兼容对象 — 给 _http_rerank 重试逻辑用。"""

    def __init__(self, status_code: int, json_data: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}
        # 构造真实的 httpx.Request 让 HTTPStatusError 序列化正常
        self.request = httpx.Request("POST", "https://newapi.example/v1/rerank")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {self.status_code}", request=self.request, response=self,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """Mock httpx.AsyncClient — 按 post 顺序吐 responses。"""

    def __init__(self, responses: list[_MockResponse], capture: list[dict] | None = None):
        self._responses = list(responses)
        self._capture = capture if capture is not None else []
        self.calls: list[dict] = self._capture

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _MockResponse:
        self._capture.append({"url": url, "json": json, "headers": headers})
        if not self._responses:
            raise AssertionError("unexpected extra post call")
        return self._responses.pop(0)


def _install_fake_httpx(monkeypatch, responses: list[_MockResponse]) -> list[dict]:
    """让 reranker_registry 内的 httpx.AsyncClient(...) 走 _FakeAsyncClient。"""
    captured: list[dict] = []

    def _factory(*_: Any, **__: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(responses, captured)

    monkeypatch.setattr(rrk_mod.httpx, "AsyncClient", _factory)
    # 同时让 sleep 不真睡，加速重试测试
    monkeypatch.setattr(rrk_mod.asyncio, "sleep", lambda *_: _async_noop())
    return captured


async def _async_noop() -> None:
    return None


# ---- schema 解析 --------------------------------------------------------


def test_rerank_parses_selfhosted_standard_response(monkeypatch):
    """标准 Cohere/Jina-style results[]，按 score 降序返回。"""
    profile = _make_profile()
    reg = _make_registry()
    func, sig = reg.build_rerank_func(profile)
    assert sig == "rerank:selfhosted-bge"

    # 上游故意返回乱序 + 多于 top_n 的项
    body = {
        "results": [
            {"index": 0, "relevance_score": 0.30},
            {"index": 1, "relevance_score": 0.95},
            {"index": 2, "relevance_score": 0.55},
        ]
    }
    captured = _install_fake_httpx(monkeypatch, [_MockResponse(200, body)])

    out = asyncio.run(func("q", ["d0", "d1", "d2"], top_n=2))
    # 期望按 score 降序、截 top_n=2 → [(1, 0.95), (2, 0.55)]
    assert out == [
        {"index": 1, "relevance_score": 0.95},
        {"index": 2, "relevance_score": 0.55},
    ]
    # 验证 URL 与 payload schema
    assert len(captured) == 1
    call = captured[0]
    assert call["url"] == "https://newapi.example/v1/rerank"
    assert call["json"]["model"] == "text-reranker"
    assert call["json"]["query"] == "q"
    assert call["json"]["documents"] == ["d0", "d1", "d2"]
    assert call["json"]["top_n"] == 2
    assert call["json"]["return_documents"] is False
    assert call["headers"]["Authorization"] == "Bearer test-key"


def test_rerank_handles_unordered_index_response(monkeypatch):
    """上游返回 index 乱序时，输出仍按 score 降序排列；index 字段保持原始位置。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    body = {
        "results": [
            {"index": 4, "relevance_score": 0.10},
            {"index": 0, "relevance_score": 0.80},
            {"index": 2, "relevance_score": 0.50},
            {"index": 1, "relevance_score": 0.65},
        ]
    }
    _install_fake_httpx(monkeypatch, [_MockResponse(200, body)])
    out = asyncio.run(func("q", ["a", "b", "c", "d", "e"], top_n=None))
    indices = [item["index"] for item in out]
    scores = [item["relevance_score"] for item in out]
    assert indices == [0, 1, 2, 4]
    assert scores == sorted(scores, reverse=True)


def test_rerank_min_score_filters_out_low_relevance(monkeypatch):
    """min_score 阈值过滤 — 低于阈值的项不应出现在结果里。"""
    profile = _make_profile(min_score=0.5)
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    body = {
        "results": [
            {"index": 0, "relevance_score": 0.95},
            {"index": 1, "relevance_score": 0.30},  # 应被过滤
            {"index": 2, "relevance_score": 0.49},  # 边界外
            {"index": 3, "relevance_score": 0.51},  # 边界内
        ]
    }
    _install_fake_httpx(monkeypatch, [_MockResponse(200, body)])
    out = asyncio.run(func("q", ["d0", "d1", "d2", "d3"], top_n=10))
    indices = [item["index"] for item in out]
    assert indices == [0, 3]


def test_rerank_min_score_override_in_build(monkeypatch):
    """build_rerank_func 的 min_score 参数应覆盖 profile.min_score。"""
    profile = _make_profile(min_score=0.0)
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile, min_score=0.7)

    body = {
        "results": [
            {"index": 0, "relevance_score": 0.65},
            {"index": 1, "relevance_score": 0.85},
        ]
    }
    _install_fake_httpx(monkeypatch, [_MockResponse(200, body)])
    out = asyncio.run(func("q", ["d0", "d1"], top_n=None))
    assert [item["index"] for item in out] == [1]


# ---- 重试 ----------------------------------------------------------------


def test_rerank_retries_on_429(monkeypatch):
    """429 触发指数退避重试；第二次返回 200 视为成功。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    responses = [
        _MockResponse(429, headers={"Retry-After": "0.01"}),
        _MockResponse(200, {"results": [{"index": 0, "relevance_score": 0.9}]}),
    ]
    captured = _install_fake_httpx(monkeypatch, responses)
    out = asyncio.run(func("q", ["d0"], top_n=1))
    assert len(captured) == 2
    assert out == [{"index": 0, "relevance_score": 0.9}]


def test_rerank_does_not_retry_on_401(monkeypatch):
    """401 / 400 类客户端错误直接抛 — 不重试，暴露配置漏洞。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    captured = _install_fake_httpx(monkeypatch, [_MockResponse(401)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(func("q", ["d0"], top_n=1))
    assert len(captured) == 1


def test_rerank_does_not_retry_on_400(monkeypatch):
    """400 同理：不重试，立刻暴露 bad request。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    captured = _install_fake_httpx(monkeypatch, [_MockResponse(400)])
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(func("q", ["d0"], top_n=1))
    assert len(captured) == 1


# ---- 早返 ----------------------------------------------------------------


def test_rerank_empty_documents_returns_empty_without_http(monkeypatch):
    """空 documents → 不调 HTTP，直接返回 []。节省一次 RTT。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    captured = _install_fake_httpx(monkeypatch, [])  # 不准备任何 response
    out = asyncio.run(func("q", [], top_n=8))
    assert out == []
    assert captured == []  # 验证完全没调 HTTP


# ---- Cohere v2 兼容 ------------------------------------------------------


def test_rerank_cohere_v2_compatible_schema(monkeypatch):
    """Cohere v2 在 return_documents=false 时只回 {index, relevance_score} —
    与 self-hosted gateway / Jina 完全一致；解析器不应区分 provider。"""
    profile = _make_profile(
        name="cohere-v3",
        base_url="https://api.cohere.com/v2",
    )
    reg = _make_registry()
    func, sig = reg.build_rerank_func(profile)
    assert sig == "rerank:cohere-v3"

    body = {
        "id": "abcd",  # Cohere 真实响应里有额外字段，应被忽略
        "results": [
            {"index": 2, "relevance_score": 0.92},
            {"index": 0, "relevance_score": 0.40},
        ],
        "meta": {"billed_units": {"search_units": 1}},
    }
    captured = _install_fake_httpx(monkeypatch, [_MockResponse(200, body)])
    out = asyncio.run(func("q", ["d0", "d1", "d2"], top_n=2))
    assert out == [
        {"index": 2, "relevance_score": 0.92},
        {"index": 0, "relevance_score": 0.40},
    ]
    assert captured[0]["url"] == "https://api.cohere.com/v2/rerank"


# ---- 异常 schema ---------------------------------------------------------


def test_rerank_malformed_response_raises(monkeypatch):
    """上游返回非预期 schema（缺 results）→ 单 profile 链重试用尽后由
    run_with_fallback 归一为 NoUpstreamAvailable（RuntimeError 子类），
    错误信息保留根因 'missing results'。"""
    profile = _make_profile()
    reg = _make_registry()
    func, _ = reg.build_rerank_func(profile)

    # 三次都返回坏 schema：重试用尽后抛
    bad_resp = [_MockResponse(200, {"data": [{"index": 0, "score": 0.5}]}) for _ in range(3)]
    _install_fake_httpx(monkeypatch, bad_resp)
    with pytest.raises(RuntimeError, match=r"missing.+results"):
        asyncio.run(func("q", ["d0"], top_n=1))


# ---- registry / profile lookup ------------------------------------------


def test_get_profile_miss_raises():
    reg = _make_registry()
    with pytest.raises(KeyError, match=r"ghost"):
        reg.get_profile("ghost")


def test_get_profile_hit():
    reg = _make_registry()
    assert reg.get_profile("selfhosted-bge").model == "text-reranker"


def test_module_level_singleton_clears(monkeypatch, tmp_path):
    """get_reranker_registry / clear_reranker_registry 单例语义。"""
    # 切到一个最小可加载的 yaml，避免 legacy fallback 触发 EMBEDDING_* 校验
    for k in ("TM_PROFILES_FILE", "WORKSPACE",
              "EMBEDDING_MODEL", "EMBEDDING_DIM", "EMBEDDING_BASE_URL",
              "EMBEDDINGS_BASE_URL", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("K", "v")
    yaml_path = tmp_path / "profiles.yaml"
    yaml_path.write_text(
        """
version: 1
embeddings:
  - name: e
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
rerankers:
  - name: rrk1
    model: rrk-model
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: e
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TM_PROFILES_FILE", str(yaml_path))

    clear_reranker_registry()
    r1 = get_reranker_registry()
    r2 = get_reranker_registry()
    assert r1 is r2
    assert r1.get_profile("rrk1").model == "rrk-model"
    clear_reranker_registry()
    r3 = get_reranker_registry()
    assert r3 is not r1
    clear_reranker_registry()
