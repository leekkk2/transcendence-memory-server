"""Phase 3 v0.9.0：embedding fallback chain + circuit breaker 端到端测试。

覆盖矩阵（plan 验证标准）：
- primary 一次 429 → fallback 接手成功
- primary 持续 429 + fallback 成功 → 后续请求直接走 fallback（primary cooling）
- primary 5 次连续 fail → breaker open；30s 内立即 raise（无 HTTP 调用）
- breaker open + 显式 reset → 下次走 primary
- 所有 profile 都 fail → raise NoUpstreamAvailable
- primary 4xx 非 429（400/401/403）→ 不 fallback，直接抛
- primary 401 → breaker 不计入失败（鉴权错不算「上游不可用」）
- primary 429 + fallback 429 → 都 mark 失败 + raise NoUpstreamAvailable
- half-open：cooling 到期后第一次请求过去
- reset_breaker on 不存在的 profile → no-op，不抛
- 多 profile 各自独立 breaker（一个的 cooling 不影响另一个）

与 test_embedding_registry.py / test_reranker_client.py 风格一致：
monkeypatch + 假 httpx 客户端拦截真实网络，控制响应序列验证行为契约。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.embedding_registry as er_mod  # noqa: E402
from scripts.embedding_registry import (  # noqa: E402
    BreakerOpen,
    EmbeddingRegistry,
    NoUpstreamAvailable,
    _BREAKER_COOLING_S,
    _BREAKER_FAIL_THRESHOLD,
    _clear_all_breakers,
    reset_breaker,
)
from scripts.profiles_loader import (  # noqa: E402
    EmbeddingProfile,
    ProfileSet,
    Route,
)


# ---- helpers ------------------------------------------------------------


def _make_profile(
    name: str,
    base_url: str | None = None,
    api_key: str = "test-key",
    dim: int = 8,
    max_retries: int = 1,
) -> EmbeddingProfile:
    """max_retries=1 让单 profile 内部不重试 → fallback 链路单步进入下一条，
    测试逻辑更清晰；要测内部重试时显式覆盖。"""
    return EmbeddingProfile(
        name=name,
        provider="openai_compatible",
        model="test-model",
        dim=dim,
        base_url=base_url or f"https://{name}.example/v1",
        api_key=api_key,
        max_token_size=8192,
        request_dim=None,
        timeout_s=10.0,
        max_retries=max_retries,
    )


def _make_registry(profiles: list[EmbeddingProfile], fallbacks: tuple[str, ...] = ()) -> EmbeddingRegistry:
    """构造一份 registry，primary = profiles[0]，其余按 fallbacks 排列。"""
    emb_dict = {p.name: p for p in profiles}
    primary = profiles[0]
    default_route = Route(embedding=primary.name, embedding_fallbacks=fallbacks)
    ps = ProfileSet(
        embeddings=emb_dict,
        rerankers={},
        routes=[],
        default_route=default_route,
    )
    return EmbeddingRegistry(ps)


class _MockResponse:
    """httpx.Response 兼容对象 — 覆盖 _http_embed_single 走的接口子集。"""

    def __init__(
        self,
        status_code: int,
        embeddings: list[list[float]] | None = None,
        headers: dict | None = None,
    ):
        self.status_code = status_code
        self._embeddings = embeddings
        self.headers = headers or {}
        self.request = httpx.Request("POST", "https://example.com/v1/embeddings")

    def json(self) -> Any:
        if self._embeddings is None:
            raise ValueError("no json body")
        return {
            "data": [
                {"index": i, "embedding": emb}
                for i, emb in enumerate(self._embeddings)
            ]
        }

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"upstream {self.status_code}", request=self.request, response=self,  # type: ignore[arg-type]
            )


class _FakeAsyncClient:
    """Mock httpx.AsyncClient — 按 URL 决定走哪条 profile 的响应队列。

    用 url-prefix-routing 是因为：fallback chain 切换 profile 时会换 url；
    单顺序队列无法区分「primary 调了 3 次失败 + fallback 调了 1 次成功」
    与「primary 调了 1 次 + fallback 调了 3 次」这种不同 chain 的混排。
    """

    # 模块级 capture 让测试断言能直接读 — 每个测试 setup 时重置
    by_url: dict[str, list[_MockResponse]] = {}
    calls: list[dict] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict, headers: dict) -> _MockResponse:
        type(self).calls.append({"url": url, "json": json, "headers": headers})
        queue = type(self).by_url.get(url)
        if not queue:
            raise AssertionError(f"unexpected post to {url!r} (no mock prepared)")
        return queue.pop(0)


def _install_fake_httpx(monkeypatch, by_url: dict[str, list[_MockResponse]]) -> list[dict]:
    """重置 _FakeAsyncClient 状态，注入 httpx + 让 asyncio.sleep 不真睡。"""
    _FakeAsyncClient.by_url = by_url
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(er_mod.httpx, "AsyncClient", _FakeAsyncClient)

    async def _noop(*_: Any) -> None:
        return None

    monkeypatch.setattr(er_mod.asyncio, "sleep", lambda *a, **k: _noop())
    return _FakeAsyncClient.calls


@pytest.fixture(autouse=True)
def _reset_breakers():
    """每个测试前后清空全局 breaker 字典 — 否则前一个 test 留下的 cooling
    会让下一个 test 莫名其妙跳过 profile。"""
    _clear_all_breakers()
    yield
    _clear_all_breakers()


def _embedding_payload(n: int, dim: int = 8) -> list[list[float]]:
    return [[float(i + j) for j in range(dim)] for i in range(n)]


# =========================================================================
# 1. fallback 基本行为
# =========================================================================


def test_fallback_on_primary_429(monkeypatch):
    """primary 一次 429 → fallback 接手成功。"""
    primary = _make_profile("primary")
    fb = _make_profile("fallback")
    reg = _make_registry([primary, fb], fallbacks=("fallback",))
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    calls = _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(429, headers={"Retry-After": "0.01"}),
        ],
        "https://fallback.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1)),
        ],
    })

    out = asyncio.run(func(["hello"]))
    assert out.shape == (1, 8)
    # primary 1 次（max_retries=1，立即抛）+ fallback 1 次成功
    assert len(calls) == 2
    assert calls[0]["url"].startswith("https://primary.example")
    assert calls[1]["url"].startswith("https://fallback.example")


def test_fallback_skips_primary_after_breaker_open(monkeypatch):
    """primary 5 次连续 fail → breaker open，下一次请求直接走 fallback，
    不再尝试 primary。"""
    primary = _make_profile("primary")
    fb = _make_profile("fallback")
    reg = _make_registry([primary, fb], fallbacks=("fallback",))
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    # 准备 5 次 primary 429 + 5 次 fallback 200
    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(429, headers={"Retry-After": "0.01"})
            for _ in range(_BREAKER_FAIL_THRESHOLD)
        ],
        "https://fallback.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1))
            for _ in range(_BREAKER_FAIL_THRESHOLD + 1)
        ],
    })

    # 跑 5 次让 primary breaker open（每次都 fallback 成功）
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        out = asyncio.run(func(["x"]))
        assert out.shape == (1, 8)

    # 此时 primary 应该 cooling — 第 6 次请求不应碰 primary
    pre_calls = len(_FakeAsyncClient.calls)
    out = asyncio.run(func(["x"]))
    new_calls = _FakeAsyncClient.calls[pre_calls:]
    assert all(c["url"].startswith("https://fallback.example") for c in new_calls), (
        f"primary should be skipped due to breaker, but was called: {new_calls}"
    )
    assert out.shape == (1, 8)


# =========================================================================
# 2. circuit breaker 状态机
# =========================================================================


def test_breaker_opens_after_consecutive_failures_no_http(monkeypatch):
    """primary 累积 5 次失败 → breaker open；30s 内调用立即 raise（无 HTTP）。"""
    primary = _make_profile("primary")
    # 没有 fallback — 5 次失败后 breaker 直接挡住后续请求
    reg = _make_registry([primary])
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(503) for _ in range(_BREAKER_FAIL_THRESHOLD)
        ],
    })

    # 头 5 次都会真打到 primary，全失败
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(func(["x"]))

    # 第 6 次：breaker open，不应碰 HTTP（mock 队列已空，碰到就 AssertionError）
    pre = len(_FakeAsyncClient.calls)
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(func(["x"]))
    assert len(_FakeAsyncClient.calls) == pre, "breaker open 时不应调 HTTP"


def test_breaker_reset_via_explicit_call(monkeypatch):
    """breaker open + 显式 reset_breaker → 下次走 primary 成功。"""
    primary = _make_profile("primary")
    reg = _make_registry([primary])
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    # 5 次失败让 breaker open
    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(503) for _ in range(_BREAKER_FAIL_THRESHOLD)
        ],
    })
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(func(["x"]))

    # 显式 reset，并准备一次 200
    assert reset_breaker("primary") is True, "reset 应返回 True (had state)"
    _FakeAsyncClient.by_url = {
        "https://primary.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1)),
        ],
    }
    out = asyncio.run(func(["x"]))
    assert out.shape == (1, 8)


def test_all_profiles_fail_raises_no_upstream_available(monkeypatch):
    """primary + fallback 都失败 → NoUpstreamAvailable，两边都 mark 失败计数。"""
    primary = _make_profile("primary")
    fb = _make_profile("fallback")
    reg = _make_registry([primary, fb], fallbacks=("fallback",))
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(429, headers={"Retry-After": "0.01"}),
        ],
        "https://fallback.example/v1/embeddings": [
            _MockResponse(429, headers={"Retry-After": "0.01"}),
        ],
    })

    with pytest.raises(NoUpstreamAvailable) as ei:
        asyncio.run(func(["x"]))
    # 错误信息必须能定位失败链路
    assert "primary" in str(ei.value)
    assert "fallback" in str(ei.value)

    # 两条 profile 各 +1 失败计数
    assert er_mod._breakers["embed:primary"].consecutive_fails == 1
    assert er_mod._breakers["embed:fallback"].consecutive_fails == 1


# =========================================================================
# 3. 错误分类：fallback-eligible vs config error
# =========================================================================


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_4xx_non_429_does_not_fallback(monkeypatch, status_code):
    """primary 4xx（非 429）→ 不 fallback、不消耗 fallback quota，直接抛
    HTTPStatusError。breaker 不计入（用户配置错，不算上游不可用）。"""
    primary = _make_profile("primary")
    fb = _make_profile("fallback")
    reg = _make_registry([primary, fb], fallbacks=("fallback",))
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [_MockResponse(status_code)],
        # 故意准备 fallback response，如果被消费说明 fallback 被错误触发
        "https://fallback.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1)),
        ],
    })

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(func(["x"]))

    # primary 调用 1 次，fallback 未被触达
    primary_calls = [c for c in _FakeAsyncClient.calls
                     if c["url"].startswith("https://primary")]
    fallback_calls = [c for c in _FakeAsyncClient.calls
                      if c["url"].startswith("https://fallback")]
    assert len(primary_calls) == 1
    assert len(fallback_calls) == 0, "非 429 错误不应 fallback"

    # breaker 不计入失败（rerun 必须仍然能访问 primary，不会被 cooling 挡住）
    state = er_mod._breakers.get("embed:primary")
    assert state is None or state.consecutive_fails == 0, (
        f"401/400/403 不应触发 breaker，但 state={state}"
    )


def test_401_does_not_trigger_breaker_even_after_many_calls(monkeypatch):
    """连续 N 次 401 → breaker 永远不 open（用户改 key 才是正解）。"""
    primary = _make_profile("primary")
    reg = _make_registry([primary])
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(401) for _ in range(_BREAKER_FAIL_THRESHOLD + 2)
        ],
    })

    for _ in range(_BREAKER_FAIL_THRESHOLD + 2):
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(func(["x"]))

    state = er_mod._breakers.get("embed:primary")
    assert state is None or state.consecutive_fails == 0


# =========================================================================
# 4. half-open & cooling timing
# =========================================================================


def test_half_open_after_cooling_expires(monkeypatch):
    """cooling 到期后下次请求过去（half-open 探活）成功 → 重置 breaker。

    时间用 monkeypatch.setattr 控制 time.monotonic，避免真睡 30s。"""
    primary = _make_profile("primary")
    reg = _make_registry([primary])
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    # 用可变 ref 控制虚拟时钟
    fake_now = [1000.0]
    monkeypatch.setattr(er_mod.time, "monotonic", lambda: fake_now[0])

    # 5 次失败让 breaker open
    _install_fake_httpx(monkeypatch, {
        "https://primary.example/v1/embeddings": [
            _MockResponse(503) for _ in range(_BREAKER_FAIL_THRESHOLD)
        ],
    })
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(func(["x"]))

    # 时钟未到 cooling — 立即 raise 不调 HTTP
    pre = len(_FakeAsyncClient.calls)
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(func(["x"]))
    assert len(_FakeAsyncClient.calls) == pre

    # 时钟跳到 cooling 之后 — 准备一次 200 让 half-open 探活成功
    fake_now[0] += _BREAKER_COOLING_S + 1
    _FakeAsyncClient.by_url = {
        "https://primary.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1)),
        ],
    }
    out = asyncio.run(func(["x"]))
    assert out.shape == (1, 8)

    # 探活成功 → state 完全重置
    state = er_mod._breakers["embed:primary"]
    assert state.consecutive_fails == 0
    assert state.cooling_until_ts == 0.0
    assert state.half_open is False


# =========================================================================
# 5. reset_breaker edge cases & 多 profile 独立性
# =========================================================================


def test_reset_breaker_on_unknown_profile_is_noop():
    """reset_breaker 对从未见过的 profile 名 → 返回 False，不抛。"""
    assert reset_breaker("never-seen-profile") is False
    # breaker 字典里仍然没有这条记录（reset 不应隐式创建）；key 为复合
    # {category}:{profile}，embedding 链路前缀 embed:。
    assert "embed:never-seen-profile" not in er_mod._breakers


def test_breakers_are_per_profile_independent(monkeypatch):
    """profile A 的 cooling 不影响 profile B — 两条独立状态。"""
    pa = _make_profile("alpha", base_url="https://alpha.example/v1")
    pb = _make_profile("beta", base_url="https://beta.example/v1")

    reg_a = _make_registry([pa])
    func_a, _ = reg_a.build_embedding_func(reg_a.resolve("any"))
    reg_b = _make_registry([pb])
    func_b, _ = reg_b.build_embedding_func(reg_b.resolve("any"))

    # 让 alpha breaker open，同时 beta 正常工作
    _install_fake_httpx(monkeypatch, {
        "https://alpha.example/v1/embeddings": [
            _MockResponse(503) for _ in range(_BREAKER_FAIL_THRESHOLD)
        ],
        "https://beta.example/v1/embeddings": [
            _MockResponse(200, embeddings=_embedding_payload(1)),
        ],
    })

    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(func_a(["x"]))

    # alpha cooling — beta 应该完全不受影响
    assert er_mod._breakers["embed:alpha"].cooling_until_ts > 0
    out = asyncio.run(func_b(["x"]))
    assert out.shape == (1, 8)
    state_b = er_mod._breakers.get("embed:beta")
    assert state_b is None or state_b.cooling_until_ts == 0.0


# =========================================================================
# 6. typed exception → _is_fallback_eligible 分类
# =========================================================================


def test_is_fallback_eligible_typed_exceptions():
    """gemini_native / registry 重试耗尽后抛的 typed exception 按 error_class 判定：
    quota / timeout / transient 可 fallback，permanent 不可。"""
    from scripts import embedding_errors as ee
    from scripts.embedding_registry import _is_fallback_eligible

    # gemini_native 的 429 现在被包成 EmbeddingQuotaError → 应判 True
    assert _is_fallback_eligible(ee.EmbeddingQuotaError("q", status=429)) is True
    assert _is_fallback_eligible(ee.EmbeddingTimeoutError("t")) is True
    assert _is_fallback_eligible(ee.EmbeddingTransientError("x", status=503)) is True
    # 4xx 非 429 永久错 —— 换 profile 没用，不 fallback
    assert _is_fallback_eligible(ee.EmbeddingPermanentError("p", status=400)) is False


def test_typed_quota_error_triggers_fallback(monkeypatch):
    """primary 抛 typed EmbeddingQuotaError（模拟 gemini_native 429 耗尽）→
    fallback 链路 catch 后切下一条 profile 成功。"""
    from scripts import embedding_errors as ee

    primary = _make_profile("primary")
    fb = _make_profile("fallback")
    reg = _make_registry([primary, fb], fallbacks=("fallback",))
    route = reg.resolve("any")
    func, _ = reg.build_embedding_func(route)

    calls: list[str] = []

    async def fake_embed(profile, texts):
        calls.append(profile.name)
        if profile.name == "primary":
            raise ee.EmbeddingQuotaError(
                "gemini embedContent failed after 2 retries", status=429, retry_after=5.0,
            )
        return np.zeros((len(texts), profile.dim), dtype="float32")

    monkeypatch.setattr(er_mod, "_http_embed", fake_embed)
    out = asyncio.run(func(["hello"]))
    assert out.shape == (1, 8)
    assert calls == ["primary", "fallback"]
    # primary 的 quota 失败计入 breaker
    assert er_mod._breakers["embed:primary"].consecutive_fails == 1
