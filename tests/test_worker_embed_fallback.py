"""链路 2 —— worker subprocess 同步 embedding fallback 测试。

覆盖矩阵：
- primary 同步 429 → fallback profile 接管成功（run_with_fallback_sync）
- primary 持续 429 → 连续失败达阈值后 breaker open，后续请求跳过 primary
- 全链挂掉 → NoUpstreamAvailable
- `AIza` 开头 key 的单点 Google native fallback 已移除 —— 主链失败后不再
  改打 generativelanguage.googleapis.com

HTTP 全程 mock 在 requests.post 层，按 url 路由响应队列。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.model_fallback import (  # noqa: E402
    _BREAKER_FAIL_THRESHOLD,
    NoUpstreamAvailable,
)
from scripts.profiles_loader import EmbeddingProfile  # noqa: E402


@pytest.fixture
def runtime(monkeypatch):
    """重载 task_rag_runtime —— 把单 profile 内部重试压到 1 次，
    fallback 链路单步推进，逻辑更清晰。"""
    monkeypatch.setenv("EMBEDDING_MAX_RETRIES", "1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    sys.modules.pop("scripts.task_rag_runtime", None)
    sys.modules.pop("task_rag_runtime", None)
    return importlib.import_module("scripts.task_rag_runtime")


def _emb(name: str, api_key: str = "test-key") -> EmbeddingProfile:
    """构造 EmbeddingProfile，base_url 以 name 为前缀便于 mock 按 url 路由。"""
    return EmbeddingProfile(
        name=name,
        provider="openai_compatible",
        model="m",
        dim=4,
        base_url=f"https://{name}.example/v1",
        api_key=api_key,
    )


class _Resp:
    """requests.Response 兼容子集。"""

    def __init__(
        self, status: int, embedding: list[float] | None = None,
        headers: dict | None = None,
    ) -> None:
        self.status_code = status
        self._embedding = embedding
        self.text = "" if embedding is not None else f"err-{status}"
        self.headers = headers or {}

    def json(self) -> Any:
        if self._embedding is None:
            raise ValueError("no json body")
        return {"data": [{"index": 0, "embedding": self._embedding}]}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self  # type: ignore[attr-defined]
            raise err


def _install(runtime, monkeypatch, by_url: dict[str, list[_Resp]]) -> list[str]:
    """注入假 requests.post（按 url 路由）+ no-op sleep。"""
    calls: list[str] = []

    def fake_post(url: str, **_: Any) -> _Resp:
        calls.append(url)
        queue = by_url.get(url)
        if not queue:
            raise AssertionError(f"unexpected post to {url!r} (no mock prepared)")
        return queue.pop(0)

    monkeypatch.setattr(runtime.requests, "post", fake_post)
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: None)
    return calls


def test_worker_sync_fallback_on_primary_429(runtime, monkeypatch):
    """primary 同步 429 → fallback profile 接管成功。"""
    primary, fb = _emb("primary"), _emb("fallback")
    monkeypatch.setattr(runtime, "_resolve_chain_for_worker", lambda: [primary, fb])

    calls = _install(runtime, monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _Resp(429, headers={"Retry-After": "0"}),
        ],
        "https://fallback.example/v1/embeddings": [
            _Resp(200, embedding=[1.0, 2.0, 3.0, 4.0]),
        ],
    })

    vec = runtime.embed_text("hello")
    assert isinstance(vec, np.ndarray)
    assert vec.shape == (4,)
    # primary 1 次失败 + fallback 1 次成功
    assert len(calls) == 2
    assert calls[0].startswith("https://primary")
    assert calls[1].startswith("https://fallback")


def test_worker_sync_breaker_opens_then_skips_primary(runtime, monkeypatch):
    """primary 持续 429：连续失败达阈值后 breaker open，后续直接走 fallback。"""
    primary, fb = _emb("primary"), _emb("fallback")
    monkeypatch.setattr(runtime, "_resolve_chain_for_worker", lambda: [primary, fb])
    n = _BREAKER_FAIL_THRESHOLD

    calls = _install(runtime, monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _Resp(429, headers={"Retry-After": "0"}) for _ in range(n)
        ],
        "https://fallback.example/v1/embeddings": [
            _Resp(200, embedding=[1.0, 2.0, 3.0, 4.0]) for _ in range(n + 1)
        ],
    })

    for _ in range(n):
        assert runtime.embed_text("x").shape == (4,)

    pre = len(calls)
    assert runtime.embed_text("x").shape == (4,)
    new_calls = calls[pre:]
    assert all(u.startswith("https://fallback") for u in new_calls), (
        f"primary should be skipped due to open breaker: {new_calls}"
    )


def test_worker_sync_all_fail_raises_no_upstream(runtime, monkeypatch):
    """primary + fallback 都 429 → NoUpstreamAvailable。"""
    primary, fb = _emb("primary"), _emb("fallback")
    monkeypatch.setattr(runtime, "_resolve_chain_for_worker", lambda: [primary, fb])

    _install(runtime, monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _Resp(429, headers={"Retry-After": "0"}),
        ],
        "https://fallback.example/v1/embeddings": [
            _Resp(429, headers={"Retry-After": "0"}),
        ],
    })

    with pytest.raises(NoUpstreamAvailable) as ei:
        runtime.embed_text("x")
    assert "primary" in str(ei.value)
    assert "fallback" in str(ei.value)


def test_worker_aiza_single_point_fallback_removed(runtime, monkeypatch):
    """`AIza` 开头 key 的单点 Google native fallback 已移除：

    主链全挂后不应改打 generativelanguage.googleapis.com，而是直接抛
    NoUpstreamAvailable —— 备用 Google endpoint 现应作为独立 gemini_native
    profile 配进 route.embedding_fallbacks。
    """
    aiza = _emb("aizaprof", api_key="AIzaSyFAKEKEYFORTEST")
    monkeypatch.setattr(runtime, "_resolve_chain_for_worker", lambda: [aiza])

    calls = _install(runtime, monkeypatch, {
        "https://aizaprof.example/v1/embeddings": [
            _Resp(429, headers={"Retry-After": "0"}),
        ],
    })

    with pytest.raises(NoUpstreamAvailable):
        runtime.embed_text("x")
    # 关键断言：没有任何请求打到 Google 原生 endpoint
    assert all("generativelanguage" not in u for u in calls), (
        f"AIza single-point Google fallback should be removed, but saw: {calls}"
    )
    assert len(calls) == 1
