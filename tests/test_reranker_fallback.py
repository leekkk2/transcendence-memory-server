"""链路 6 —— reranker fallback chain 测试（build_rerank_func + run_with_fallback）。

覆盖矩阵：
- primary reranker 503 → fallback profile 接管成功
- primary 持续 503 → 连续失败达阈值后 breaker open，后续请求跳过 primary
- 全链挂掉 → NoUpstreamAvailable
- 全链挂掉时上层（/search 的 try/except）据此降级为无 rerank、检索仍返回

HTTP 全程 mock 在 httpx.AsyncClient 层，按 base_url 路由响应队列。
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

import scripts.reranker_registry as rrk_mod  # noqa: E402
from scripts.model_fallback import (  # noqa: E402
    _BREAKER_FAIL_THRESHOLD,
    NoUpstreamAvailable,
)
from scripts.profiles_loader import ProfileSet, RerankerProfile, Route  # noqa: E402
from scripts.reranker_registry import RerankerRegistry  # noqa: E402


def _rrk(name: str) -> RerankerProfile:
    """构造 RerankerProfile，base_url 以 name 为前缀便于 mock 按 url 路由。"""
    return RerankerProfile(
        name=name,
        provider="cohere_compatible",
        model="text-reranker",
        base_url=f"https://{name}.example/v1",
        api_key="test-key",
        timeout_s=10.0,
        min_score=0.0,
    )


def _registry() -> RerankerRegistry:
    """build_rerank_func 不依赖实例状态 —— 空 ProfileSet 足够构造 registry。"""
    ps = ProfileSet(
        embeddings={},  # type: ignore[arg-type]
        rerankers={},
        routes=[],
        default_route=Route(embedding="anything"),
    )
    return RerankerRegistry(ps)


class _MockResp:
    """httpx.Response 兼容子集 —— 覆盖 _http_rerank 走的接口。"""

    def __init__(self, status_code: int, results: list[dict] | None = None) -> None:
        self.status_code = status_code
        self._results = results
        self.headers: dict[str, str] = {}
        self.request = httpx.Request("POST", "https://example.com/v1/rerank")

    def json(self) -> Any:
        if self._results is None:
            raise ValueError("no json body")
        return {"results": self._results}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """按 url 决定响应队列的假 httpx 客户端。"""

    by_url: dict[str, list[_MockResp]] = {}
    calls: list[str] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _MockResp:
        type(self).calls.append(url)
        queue = type(self).by_url.get(url)
        if not queue:
            raise AssertionError(f"unexpected post to {url!r} (no mock prepared)")
        return queue.pop(0)


def _install(monkeypatch, by_url: dict[str, list[_MockResp]]) -> list[str]:
    """注入假 httpx + 把单 profile 内部重试压到 1 次（fallback 链路单步推进）。"""
    _FakeAsyncClient.by_url = by_url
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(rrk_mod.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(rrk_mod, "_DEFAULT_RERANK_MAX_RETRIES", 1)
    return _FakeAsyncClient.calls


def test_rerank_fallback_on_primary_503(monkeypatch):
    """primary reranker 503 → fallback profile 接管成功。"""
    func, sig = _registry().build_rerank_func([_rrk("primary"), _rrk("fallback")])
    assert sig == "rerank:primary+fallback"

    calls = _install(monkeypatch, {
        "https://primary.example/v1/rerank": [_MockResp(503)],
        "https://fallback.example/v1/rerank": [_MockResp(200, results=[
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.4},
        ])],
    })

    out = asyncio.run(func("q", ["doc-a", "doc-b"], top_n=2))
    # fallback 成功 —— 按 score 降序回传
    assert out == [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.4},
    ]
    assert len(calls) == 2
    assert calls[0].startswith("https://primary")
    assert calls[1].startswith("https://fallback")


def test_rerank_breaker_opens_after_persistent_503(monkeypatch):
    """primary 持续 503：连续失败达阈值后 breaker open，后续直接走 fallback。"""
    func, _ = _registry().build_rerank_func([_rrk("primary"), _rrk("fallback")])
    n = _BREAKER_FAIL_THRESHOLD

    _install(monkeypatch, {
        "https://primary.example/v1/rerank": [_MockResp(503) for _ in range(n)],
        "https://fallback.example/v1/rerank": [
            _MockResp(200, results=[{"index": 0, "relevance_score": 0.5}])
            for _ in range(n + 1)
        ],
    })

    for _ in range(n):
        assert asyncio.run(func("q", ["d"], top_n=1))

    pre = len(_FakeAsyncClient.calls)
    asyncio.run(func("q", ["d"], top_n=1))
    new_calls = _FakeAsyncClient.calls[pre:]
    assert all(u.startswith("https://fallback") for u in new_calls), (
        f"primary should be skipped due to open breaker: {new_calls}"
    )


def test_rerank_all_profiles_fail_raises_no_upstream(monkeypatch):
    """primary + fallback 都 503 → NoUpstreamAvailable，错误信息含整条链。"""
    func, _ = _registry().build_rerank_func([_rrk("primary"), _rrk("fallback")])
    _install(monkeypatch, {
        "https://primary.example/v1/rerank": [_MockResp(503)],
        "https://fallback.example/v1/rerank": [_MockResp(503)],
    })

    with pytest.raises(NoUpstreamAvailable) as ei:
        asyncio.run(func("q", ["d"], top_n=1))
    assert "primary" in str(ei.value)
    assert "fallback" in str(ei.value)


def test_rerank_all_fail_degrades_to_no_rerank(monkeypatch):
    """全链挂掉抛 NoUpstreamAvailable（Exception 子类）—— /search 的
    `try/except Exception` 据此降级为无 rerank、返回原始向量检索结果。

    本测试就地模拟 search() 的降级分支，验证降级路径仍成立。
    """
    func, _ = _registry().build_rerank_func([_rrk("primary"), _rrk("fallback")])
    _install(monkeypatch, {
        "https://primary.example/v1/rerank": [_MockResp(503)],
        "https://fallback.example/v1/rerank": [_MockResp(503)],
    })

    vector_hits = ["doc-a", "doc-b", "doc-c"]
    try:
        ranked = asyncio.run(func("q", vector_hits, top_n=2))
    except Exception:  # search() 用同款 `except Exception` 兜底降级
        ranked = vector_hits[:2]
    # rerank 全挂 → 退回未重排的向量结果，检索不报错、仍返回
    assert ranked == ["doc-a", "doc-b"]
