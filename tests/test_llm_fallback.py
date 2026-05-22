"""LLM chat fallback chain 端到端测试（rag_engine.make_llm_func + run_with_fallback）。

覆盖矩阵：
- primary LLM 一次 429 → fallback profile 接管成功
- primary 持续 429 → 连续失败达阈值后 circuit breaker open，后续请求跳过 primary
- primary 401 鉴权错 → 不前进 fallback，原样上抛 HTTPStatusError
- 全链挂掉（都 5xx）→ NoUpstreamAvailable，错误信息含整条链

HTTP 全程 mock 在 httpx.AsyncClient 层，按 base_url 路由响应队列 —— 与
test_embedding_fallback 同款 url-prefix-routing 模式。
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

import scripts.rag_engine as rag_mod  # noqa: E402
from scripts.model_fallback import (  # noqa: E402
    _BREAKER_FAIL_THRESHOLD,
    NoUpstreamAvailable,
)
from scripts.profiles_loader import LLMProfile  # noqa: E402

_URL = "/chat/completions"


def _llm(name: str) -> LLMProfile:
    """构造一条 LLMProfile，base_url 以 name 为前缀便于 mock 按 url 路由。"""
    return LLMProfile(
        name=name,
        model="test-llm",
        base_url=f"https://{name}.example/v1",
        api_key="test-key",
        timeout_s=10.0,
        max_retries=1,
    )


class _MockResp:
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
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(rag_mod, "_LLM_MAX_RETRIES", 1)
    return _FakeAsyncClient.calls


def test_llm_fallback_on_primary_429(monkeypatch):
    """primary 一次 429 → fallback 接管，返回 fallback 的 content。"""
    chain = [_llm("primary-llm"), _llm("backup-llm")]
    func = rag_mod.make_llm_func(chain)
    calls = _install(monkeypatch, {
        f"https://primary-llm.example/v1{_URL}": [_MockResp(429)],
        f"https://backup-llm.example/v1{_URL}": [
            _MockResp(200, content="hi from backup"),
        ],
    })

    out = asyncio.run(func("question"))
    assert out == "hi from backup"
    # primary 1 次失败 + backup 1 次成功
    assert len(calls) == 2
    assert calls[0].startswith("https://primary-llm")
    assert calls[1].startswith("https://backup-llm")


def test_llm_breaker_opens_after_persistent_429(monkeypatch):
    """primary 持续 429：连续失败达阈值后 breaker open，后续请求直接走 backup。"""
    chain = [_llm("primary-llm"), _llm("backup-llm")]
    func = rag_mod.make_llm_func(chain)
    n = _BREAKER_FAIL_THRESHOLD
    _install(monkeypatch, {
        f"https://primary-llm.example/v1{_URL}": [_MockResp(429) for _ in range(n)],
        f"https://backup-llm.example/v1{_URL}": [
            _MockResp(200, content="ok") for _ in range(n + 1)
        ],
    })

    # 跑 n 次让 primary breaker open（每次都 backup 兜底成功）
    for _ in range(n):
        assert asyncio.run(func("q")) == "ok"

    # 第 n+1 次：primary cooling，不应再被调用
    pre = len(_FakeAsyncClient.calls)
    assert asyncio.run(func("q")) == "ok"
    new_calls = _FakeAsyncClient.calls[pre:]
    assert all("backup-llm" in u for u in new_calls), (
        f"primary should be skipped due to open breaker: {new_calls}"
    )


def test_llm_401_does_not_fallback(monkeypatch):
    """primary 401 鉴权错 → 不前进 fallback、不消耗 backup quota，原样抛。"""
    chain = [_llm("primary-llm"), _llm("backup-llm")]
    func = rag_mod.make_llm_func(chain)
    calls = _install(monkeypatch, {
        f"https://primary-llm.example/v1{_URL}": [_MockResp(401)],
        f"https://backup-llm.example/v1{_URL}": [
            _MockResp(200, content="should-not-reach"),
        ],
    })

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(func("q"))
    assert len(calls) == 1
    assert all("primary-llm" in u for u in calls), "401 不应触达 backup"


def test_llm_all_profiles_fail_raises_no_upstream(monkeypatch):
    """primary + backup 都 5xx → NoUpstreamAvailable，错误信息含整条链。"""
    chain = [_llm("primary-llm"), _llm("backup-llm")]
    func = rag_mod.make_llm_func(chain)
    _install(monkeypatch, {
        f"https://primary-llm.example/v1{_URL}": [_MockResp(503)],
        f"https://backup-llm.example/v1{_URL}": [_MockResp(503)],
    })

    with pytest.raises(NoUpstreamAvailable) as ei:
        asyncio.run(func("q"))
    assert "primary-llm" in str(ei.value)
    assert "backup-llm" in str(ei.value)
