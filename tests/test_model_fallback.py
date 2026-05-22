"""通用模型 fallback 核心（model_fallback + model_errors）端到端测试。

覆盖矩阵：
- 单 profile 成功 / 链路按顺序推进
- breaker open 跳过 / cooling 内不调 executor
- 全挂 → NoUpstreamAvailable（错误信息含链路）
- 错误分类决定前进：429 / 5xx / typed quota 前进；401 / 400 / context-overflow
  不前进直接上抛
- half-open 探活：cooling 到期后放一次请求过去，成功则 reset
- run_with_fallback_sync 与 async 版逻辑镜像
- 复合 breaker key 跨 category 隔离：embed:p1 与 llm:p1 互不影响
- model_errors 泛化：context-overflow 归 permanent、旧名别名同一 class object
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

import scripts.model_fallback as mf  # noqa: E402
from scripts.model_errors import (  # noqa: E402
    AUTH,
    PERMANENT,
    RETRYABLE_CLASSES,
    ModelPermanentError,
    ModelQuotaError,
    ModelTimeoutError,
    classify_model_error,
    is_context_overflow,
)
from scripts.model_fallback import (  # noqa: E402
    NoUpstreamAvailable,
    _BREAKER_COOLING_S,
    _BREAKER_FAIL_THRESHOLD,
    reset_breaker,
    run_with_fallback,
    run_with_fallback_sync,
)


# ---- helpers ------------------------------------------------------------
class _Dummy:
    """最小 profile 替身 —— runner 只读 .name。"""

    def __init__(self, name: str) -> None:
        self.name = name


def _p(name: str) -> _Dummy:
    return _Dummy(name)


def _http_error(status: int) -> httpx.HTTPStatusError:
    """构造一个带指定状态码的 httpx.HTTPStatusError。"""
    req = httpx.Request("POST", "https://example.com/v1")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"upstream {status}", request=req, response=resp)


class ProgrammableExecutor:
    """可编程 executor —— 按 profile 名维护结果队列，第 N 次调用 pop 第 N 项。

    队列项：``BaseException`` 实例 → 抛出；其它值 → 作为结果返回。
    """

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls: list[str] = []

    def _next(self, profile: _Dummy) -> Any:
        self.calls.append(profile.name)
        queue = self.scripts.get(profile.name)
        if not queue:
            raise AssertionError(f"no outcome scripted for profile {profile.name!r}")
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def aexec(self, profile: _Dummy) -> Any:
        return self._next(profile)

    def sexec(self, profile: _Dummy) -> Any:
        return self._next(profile)


@pytest.fixture(autouse=True)
def _reset_breakers():
    """每个测试前后清空全局 breaker 字典 —— 隔离测试间状态。"""
    mf._clear_all_breakers()
    yield
    mf._clear_all_breakers()


# =========================================================================
# 1. 链路推进基本行为
# =========================================================================
def test_single_profile_success():
    """chain 单元素 → runner 退化为单次调用。"""
    ex = ProgrammableExecutor({"p1": ["ok"]})
    out = asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    assert out == "ok"
    assert ex.calls == ["p1"]


def test_advances_to_fallback_on_quota():
    """primary 抛 quota 错 → 按 chain 顺序切下一条成功。"""
    ex = ProgrammableExecutor({
        "p1": [ModelQuotaError("quota exhausted", status=429)],
        "p2": ["from-fallback"],
    })
    out = asyncio.run(run_with_fallback("embed", [_p("p1"), _p("p2")], ex.aexec))
    assert out == "from-fallback"
    assert ex.calls == ["p1", "p2"]


def test_advances_through_multiple_fallbacks():
    """链路逐条推进直到命中可用 profile。"""
    ex = ProgrammableExecutor({
        "p1": [_http_error(503)],
        "p2": [ModelTimeoutError("slow")],
        "p3": ["third-wins"],
    })
    chain = [_p("p1"), _p("p2"), _p("p3")]
    out = asyncio.run(run_with_fallback("llm", chain, ex.aexec))
    assert out == "third-wins"
    assert ex.calls == ["p1", "p2", "p3"]


def test_all_fail_raises_no_upstream_available():
    """全部 profile 失败 → NoUpstreamAvailable，错误信息含链路名。"""
    ex = ProgrammableExecutor({
        "p1": [_http_error(503)],
        "p2": [_http_error(500)],
    })
    with pytest.raises(NoUpstreamAvailable) as ei:
        asyncio.run(run_with_fallback("embed", [_p("p1"), _p("p2")], ex.aexec))
    assert "p1" in str(ei.value)
    assert "p2" in str(ei.value)


def test_empty_chain_raises():
    ex = ProgrammableExecutor({})
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(run_with_fallback("embed", [], ex.aexec))


# =========================================================================
# 2. circuit breaker
# =========================================================================
def test_breaker_opens_and_skips_executor():
    """单 profile 连续失败达阈值 → breaker open，后续调用不再触达 executor。"""
    ex = ProgrammableExecutor({"p1": [_http_error(503)] * _BREAKER_FAIL_THRESHOLD})
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    pre = len(ex.calls)
    # breaker open —— 这一次应直接跳过，不调 executor（脚本队列已空，碰到即报错）
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    assert len(ex.calls) == pre, "breaker open 时不应调用 executor"


def test_breaker_open_skips_to_fallback():
    """primary breaker open 后，后续请求直接走 fallback。"""
    chain = [_p("p1"), _p("p2")]
    ex = ProgrammableExecutor({
        "p1": [_http_error(503)] * _BREAKER_FAIL_THRESHOLD,
        "p2": ["ok"] * (_BREAKER_FAIL_THRESHOLD + 1),
    })
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        assert asyncio.run(run_with_fallback("embed", chain, ex.aexec)) == "ok"
    pre = len(ex.calls)
    asyncio.run(run_with_fallback("embed", chain, ex.aexec))
    new_calls = ex.calls[pre:]
    assert new_calls == ["p2"], f"primary 应被 breaker 跳过，实际: {new_calls}"


def test_reset_breaker_clears_state():
    """reset_breaker 显式拔保险丝；未知 profile no-op 返回 False。"""
    ex = ProgrammableExecutor({"p1": [_http_error(503)] * _BREAKER_FAIL_THRESHOLD})
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    assert reset_breaker("embed", "p1") is True
    assert reset_breaker("embed", "never-seen") is False
    # reset 后再调用应重新走 executor
    ex2 = ProgrammableExecutor({"p1": ["ok"]})
    assert asyncio.run(run_with_fallback("embed", [_p("p1")], ex2.aexec)) == "ok"


def test_half_open_probe_success_resets(monkeypatch):
    """cooling 到期后下一次请求过去（half-open 探活）成功 → breaker 完全重置。"""
    fake_now = [1000.0]
    monkeypatch.setattr(mf.time, "monotonic", lambda: fake_now[0])

    ex = ProgrammableExecutor({"p1": [_http_error(503)] * _BREAKER_FAIL_THRESHOLD})
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    assert mf._breakers["embed:p1"].cooling_until_ts > 0

    # cooling 未到期 —— 跳过，不调 executor
    pre = len(ex.calls)
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(run_with_fallback("embed", [_p("p1")], ex.aexec))
    assert len(ex.calls) == pre

    # 时钟跳过 cooling —— half-open 探活成功
    fake_now[0] += _BREAKER_COOLING_S + 1
    ex_ok = ProgrammableExecutor({"p1": ["recovered"]})
    assert asyncio.run(run_with_fallback("embed", [_p("p1")], ex_ok.aexec)) == "recovered"
    state = mf._breakers["embed:p1"]
    assert state.consecutive_fails == 0
    assert state.cooling_until_ts == 0.0
    assert state.half_open is False


# =========================================================================
# 3. 错误分类决定是否前进
# =========================================================================
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_non_429_does_not_advance(status):
    """4xx 非 429 → 不前进、直接上抛原始异常，fallback 不被触达。"""
    ex = ProgrammableExecutor({
        "p1": [_http_error(status)],
        "p2": ["should-not-be-reached"],
    })
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(run_with_fallback("embed", [_p("p1"), _p("p2")], ex.aexec))
    assert ex.calls == ["p1"], "非 429 错误不应触达 fallback"
    # 不计入 breaker（用户配置错，不是上游不可用）
    assert "embed:p1" not in mf._breakers or mf._breakers["embed:p1"].consecutive_fails == 0


def test_context_overflow_does_not_advance():
    """context-overflow → 换模型也救不了，直接上抛、不前进。"""
    ex = ProgrammableExecutor({
        "p1": [ValueError("maximum context length exceeded for this input")],
        "p2": ["should-not-be-reached"],
    })
    with pytest.raises(ValueError):
        asyncio.run(run_with_fallback("llm", [_p("p1"), _p("p2")], ex.aexec))
    assert ex.calls == ["p1"]


def test_permanent_error_does_not_advance():
    """typed ModelPermanentError → 不前进。"""
    ex = ProgrammableExecutor({
        "p1": [ModelPermanentError("bad request", status=400)],
        "p2": ["should-not-be-reached"],
    })
    with pytest.raises(ModelPermanentError):
        asyncio.run(run_with_fallback("llm", [_p("p1"), _p("p2")], ex.aexec))
    assert ex.calls == ["p1"]


def test_is_fallback_eligible_classification():
    """_is_fallback_eligible：429/5xx/transport/typed-retryable 前进；
    4xx 非 429 / permanent / context-overflow 不前进。"""
    elig = mf._is_fallback_eligible
    assert elig(_http_error(429)) is True
    assert elig(_http_error(503)) is True
    assert elig(httpx.ConnectError("dns")) is True
    assert elig(ModelQuotaError("q", status=429)) is True
    assert elig(ModelTimeoutError("t")) is True
    assert elig(_http_error(401)) is False
    assert elig(_http_error(400)) is False
    assert elig(ModelPermanentError("p", status=400)) is False
    assert elig(ValueError("maximum context length exceeded")) is False


# =========================================================================
# 4. sync runner 镜像 async
# =========================================================================
def test_sync_runner_advances_like_async():
    """run_with_fallback_sync 与 async 版逻辑一致：链路推进。"""
    ex = ProgrammableExecutor({
        "p1": [_http_error(503)],
        "p2": ["sync-fallback-ok"],
    })
    out = run_with_fallback_sync("embed", [_p("p1"), _p("p2")], ex.sexec)
    assert out == "sync-fallback-ok"
    assert ex.calls == ["p1", "p2"]


def test_sync_runner_breaker_and_non_eligible():
    """sync 版同样尊重 breaker 与 4xx 非 429 直接上抛。"""
    ex = ProgrammableExecutor({"p1": [_http_error(401)]})
    with pytest.raises(httpx.HTTPStatusError):
        run_with_fallback_sync("embed", [_p("p1")], ex.sexec)
    # 非 fallback 错误不计 breaker
    assert "embed:p1" not in mf._breakers or mf._breakers["embed:p1"].consecutive_fails == 0


def test_sync_and_async_share_breaker_dict():
    """sync 与 async runner 共用同一份 _breakers dict —— 一方累计的失败另一方可见。"""
    ex_async = ProgrammableExecutor({"p1": [_http_error(503)]})
    with pytest.raises(NoUpstreamAvailable):
        asyncio.run(run_with_fallback("embed", [_p("p1")], ex_async.aexec))
    ex_sync = ProgrammableExecutor({"p1": [_http_error(503)]})
    with pytest.raises(NoUpstreamAvailable):
        run_with_fallback_sync("embed", [_p("p1")], ex_sync.sexec)
    # 两次失败累计到同一条 breaker 状态
    assert mf._breakers["embed:p1"].consecutive_fails == 2


# =========================================================================
# 5. 复合 breaker key 跨 category 隔离
# =========================================================================
def test_composite_breaker_key_isolates_categories():
    """embed:p1 与 llm:p1 是两条独立 breaker —— 同名 profile 不串号。"""
    # 让 embed:p1 熔断
    ex_fail = ProgrammableExecutor({"p1": [_http_error(503)] * _BREAKER_FAIL_THRESHOLD})
    for _ in range(_BREAKER_FAIL_THRESHOLD):
        with pytest.raises(NoUpstreamAvailable):
            asyncio.run(run_with_fallback("embed", [_p("p1")], ex_fail.aexec))
    assert mf._breakers["embed:p1"].cooling_until_ts > 0

    # 同名 profile 在 llm category 下完全不受影响
    ex_ok = ProgrammableExecutor({"p1": ["llm-ok"]})
    assert asyncio.run(run_with_fallback("llm", [_p("p1")], ex_ok.aexec)) == "llm-ok"
    assert mf._breakers["llm:p1"].cooling_until_ts == 0.0
    assert mf._breakers["llm:p1"].consecutive_fails == 0


# =========================================================================
# 6. model_errors 泛化校验
# =========================================================================
def test_context_overflow_classified_permanent():
    assert is_context_overflow("request exceeds maximum context length") is True
    assert is_context_overflow("ordinary timeout") is False
    assert classify_model_error(ValueError("context_length_exceeded")) == PERMANENT


def test_auth_in_retryable_classes():
    """auth 归 retryable —— 多模型 fallback 链换 profile 可带不同凭证。"""
    assert AUTH in RETRYABLE_CLASSES


def test_legacy_embedding_error_names_are_same_class_object():
    """embedding_errors 旧名与 model_errors 新名是同一 class object。"""
    from scripts import embedding_errors as ee
    from scripts import model_errors as me

    assert ee.EmbeddingError is me.ModelError
    assert ee.EmbeddingQuotaError is me.ModelQuotaError
    assert ee.classify_embedding_error is me.classify_model_error
    # isinstance 跨新旧名成立
    assert isinstance(me.ModelQuotaError("q"), ee.EmbeddingError)
