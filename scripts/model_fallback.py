#!/usr/bin/env python3
"""通用模型 fallback 核心 —— 跨类别（embedding / LLM / VLM / reranker）复用的
熔断器 + 泛型 fallback runner。

为什么需要：每条模型链路都面临同一个问题 —— 主模型挂了要按顺序切备用模型，
且要避免对持续不可用的上游反复打 HTTP。把「熔断 + 链路推进 + 错误分类」抽成
一份与具体协议无关的核心，各链路只需注入一个 per-profile 的 executor 回调。

设计要点：

- **复合 breaker key** ``{category}:{profile_name}`` —— 同一份 ``_breakers``
  dict 跨模型类别共用。同名 profile 在 ``embed`` 与 ``llm`` 两个 category 下
  互不影响（``embed:p1`` 与 ``llm:p1`` 是两条独立状态）。
- **熔断状态机** closed → open → half-open → closed：滚动窗口内连续失败达阈值
  → open（cooling 期直接跳过，不调 HTTP）；cooling 到期 → half-open（放一次
  探活请求过去）；探活成功 reset、失败回 open。
- **同步 + async 双形态** 共用同一套熔断函数与同一份 ``_breakers`` dict ——
  server 协程链路用 async runner，worker subprocess 用 sync runner。
- **错误分类决定前进与否**：quota / timeout / transient / auth 与 429 / 5xx /
  传输错可前进 fallback；4xx 非 429、permanent、context-overflow 直接上抛。
- **零配置 = 零行为变化**：chain 只有一个元素时 runner 退化为单次调用。

R8：纯通用开源代码，不含任何 endpoint / 主机名 / 凭证。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

import httpx

# 模块身份归一：worker subprocess（bare import）与 server（package import）
# 两种入口必须共用同一份 model_errors 类对象，否则 typed exception 的跨副本
# isinstance 会失效。
model_errors = (
    sys.modules.get('scripts.model_errors')
    or sys.modules.get('model_errors')
)
if model_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import model_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import model_errors  # type: ignore[no-redef]
sys.modules.setdefault('model_errors', model_errors)
sys.modules.setdefault('scripts.model_errors', model_errors)

logger = logging.getLogger(__name__)

T = TypeVar('T')

# 模型 category —— 仅用于复合 breaker key 与日志，不做强校验（新增类别零成本）。
CATEGORY_EMBED = 'embed'
CATEGORY_LLM = 'llm'
CATEGORY_VLM = 'vlm'
CATEGORY_RERANK = 'rerank'


def _env_num(name: str, default: float, cast: Callable[[str], float]) -> float:
    """读数值型 env，缺失 / 空 / 非法都退回 default —— 运维调参不必重新打包。"""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


# Circuit breaker 参数 —— 默认值可被同名 env 覆盖。
# 选 5 / 60s / 30s：上游短时抽风 ≤ 3-4 次能自愈，5 次定性为持续不可用；30s
# cooling 比 60s 窗口短，确保偶发抽风快速恢复，又不至于刚 reset 就再次打爆
# 上游。
_BREAKER_FAIL_THRESHOLD = int(_env_num('BREAKER_FAIL_THRESHOLD', 5, int))
_BREAKER_WINDOW_S = _env_num('BREAKER_WINDOW_S', 60.0, float)
_BREAKER_COOLING_S = _env_num('BREAKER_COOLING_S', 30.0, float)


# ---- 异常类型 -----------------------------------------------------------
class NoUpstreamAvailable(RuntimeError):
    """一条链路上所有 primary + fallback 都不可用时抛出。

    区别于单点 HTTP 错误 —— 让上层能识别为「整条链路熔断」而非「某次请求
    失败」，从而走降级路径（如检索跳过 rerank）而不是直接报错。
    """


class BreakerOpen(RuntimeError):
    """单个 profile 在 cooling 期间被请求时抛出。fallback 链路 catch 此异常
    跳到下一条 profile；最后一条仍 open 时升级为 NoUpstreamAvailable。"""


# ---- circuit breaker 状态机 ---------------------------------------------
@dataclass
class BreakerState:
    """Per-profile breaker 状态。closed → open → half-open → closed 状态机。

    - consecutive_fails: 滚动窗口内连续失败计数
    - last_fail_ts: 最近一次失败时间戳（窗口滚动判定用）
    - cooling_until_ts: > now 时 open；<= now 时为 half-open（允许探活）
    - half_open: True 时下一次请求是探活请求；成功 reset，失败回 open
    """

    consecutive_fails: int = 0
    last_fail_ts: float = 0.0
    cooling_until_ts: float = 0.0
    half_open: bool = False


# 模块级 breaker 字典 + 锁 —— 跨协程 / 跨线程并发安全。key 为复合
# {category}:{profile_name}，故跨模型类别共用同一份 dict 不会串号。
_breakers: dict[str, BreakerState] = {}
_breakers_lock = threading.Lock()


def _breaker_key(category: str, profile_name: str) -> str:
    """复合 breaker key —— 同名 profile 在不同 category 下隔离。"""
    return f"{category}:{profile_name}"


def _get_breaker(key: str) -> BreakerState:
    """惰性初始化 —— 第一次见到的 key 自动建一条 closed 状态记录。"""
    with _breakers_lock:
        state = _breakers.get(key)
        if state is None:
            state = BreakerState()
            _breakers[key] = state
        return state


def _breaker_should_skip(key: str) -> bool:
    """判定是否绕过该 profile：open 且 cooling 未到期 → True；
    cooling 到期 → 自动切 half-open（放下一次请求过去），返回 False。"""
    state = _get_breaker(key)
    now = time.monotonic()
    with _breakers_lock:
        if state.cooling_until_ts > 0 and now < state.cooling_until_ts:
            return True  # 真 open，跳过
        if state.cooling_until_ts > 0 and now >= state.cooling_until_ts:
            # cooling 到期 —— 进入 half-open（cooling_until_ts 暂不清零，等
            # mark_success / mark_failure 决定下一步）
            state.half_open = True
    return False


def _breaker_mark_failure(key: str) -> None:
    """记一次失败 —— 累加计数；half-open 时直接重新 open；窗口内达阈值 → open。"""
    state = _get_breaker(key)
    now = time.monotonic()
    with _breakers_lock:
        # 窗口外的失败重置计数 —— 窗口之前的失败不参与本轮判定
        if now - state.last_fail_ts > _BREAKER_WINDOW_S:
            state.consecutive_fails = 0
        state.consecutive_fails += 1
        state.last_fail_ts = now
        if state.half_open:
            # half-open 探活失败 → 立刻回到 open，cooling 重新计时
            state.cooling_until_ts = now + _BREAKER_COOLING_S
            state.half_open = False
            logger.warning(
                "circuit breaker %s half-open probe failed, "
                "cooling another %.1fs", key, _BREAKER_COOLING_S,
            )
        elif state.consecutive_fails >= _BREAKER_FAIL_THRESHOLD:
            state.cooling_until_ts = now + _BREAKER_COOLING_S
            logger.warning(
                "circuit breaker %s opened after %d consecutive failures, "
                "cooling %.1fs", key, state.consecutive_fails, _BREAKER_COOLING_S,
            )


def _breaker_mark_success(key: str) -> None:
    """记一次成功 —— half-open 探活成功或正常成功，都重置计数。"""
    state = _get_breaker(key)
    with _breakers_lock:
        if state.cooling_until_ts > 0 or state.consecutive_fails > 0:
            logger.info(
                "circuit breaker %s reset after success (prev fails=%d)",
                key, state.consecutive_fails,
            )
        state.consecutive_fails = 0
        state.last_fail_ts = 0.0
        state.cooling_until_ts = 0.0
        state.half_open = False


def reset_breaker(category: str, profile_name: str) -> bool:
    """显式清空指定 profile 的 breaker 状态。

    供探活端点在确认上游恢复后调用 —— 让运维能手动「拔保险丝」，不必等
    cooling 自动 half-open。未知 profile no-op，不抛异常。

    Returns:
        True 表示重置了一条已存在的非空 breaker 状态；False 表示该 profile
        从未触发过 breaker（即「无需重置」）。
    """
    key = _breaker_key(category, profile_name)
    with _breakers_lock:
        state = _breakers.get(key)
        if state is None:
            return False
        had_state = (
            state.consecutive_fails > 0
            or state.cooling_until_ts > 0
            or state.half_open
        )
        state.consecutive_fails = 0
        state.last_fail_ts = 0.0
        state.cooling_until_ts = 0.0
        state.half_open = False
    if had_state:
        logger.info("circuit breaker %s explicitly reset", key)
    return had_state


def _clear_all_breakers() -> None:
    """测试专用：清空全部 breaker 状态。"""
    with _breakers_lock:
        _breakers.clear()


# ---- 错误分类：是否值得跨 profile fallback ------------------------------
def _is_fallback_eligible(exc: BaseException) -> bool:
    """判定异常是否值得跨 profile fallback。

    可前进（上游不可用，换一家可能成功）：
    - typed ModelError：error_class ∈ RETRYABLE_CLASSES（quota/timeout/
      transient/auth）
    - 429 Too Many Requests / 5xx Server Error
    - httpx.TransportError（网络层抖动 / DNS / TCP）
    - ValueError（响应体损坏 / JSON 错 —— 等价上游异常）

    不可前进（换 profile 也没用，直接上抛）：
    - context-overflow（输入超过上下文窗口 —— 换模型救不了超长输入）
    - 4xx 非 429（400/401/403/404/422 等配置或代码问题）
    - typed ModelPermanentError
    """
    # context-overflow 优先判定 —— 即便包成 ValueError 也不应前进
    if model_errors.is_context_overflow(exc):
        return False
    if isinstance(exc, model_errors.ModelError):
        return exc.error_class in model_errors.RETRYABLE_CLASSES
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    if isinstance(exc, (httpx.TransportError, ValueError)):
        return True
    return False


# ---- 泛型 fallback runner —— async / sync 双形态 ------------------------
def _should_skip_profile(category: str, profile, skipped: list[str]) -> bool:
    """breaker open 检查 —— open 时记入 skipped 并返回 True（跳过该 profile）。"""
    if _breaker_should_skip(_breaker_key(category, profile.name)):
        skipped.append(profile.name)
        logger.warning(
            "skipping %s profile=%s (circuit breaker open); trying next",
            category, profile.name,
        )
        return True
    return False


def _account_failure(category: str, profile, exc: BaseException) -> bool:
    """记一次执行失败。

    Returns:
        True  —— 可前进 fallback（已累加 breaker 失败计数）。
        False —— 用户配置错 / 永久错，调用方应直接上抛、不计入 breaker
                 （这不是「上游不可用」，是「我们配错了」）。
    """
    if not _is_fallback_eligible(exc):
        logger.warning(
            "%s profile=%s raised non-fallback error %s; not trying fallbacks",
            category, profile.name, exc,
        )
        return False
    _breaker_mark_failure(_breaker_key(category, profile.name))
    logger.warning(
        "%s profile=%s failed: %s; trying next in chain",
        category, profile.name, exc,
    )
    return True


def _on_success(category: str, idx: int, chain: list, profile, primary_label: str) -> None:
    """记一次成功并重置 breaker；命中 fallback 时 warn 级日志便于运维监控。"""
    _breaker_mark_success(_breaker_key(category, profile.name))
    if idx > 0:
        logger.warning(
            "%s fallback succeeded: profile=%s (primary=%s failed)",
            category, profile.name, primary_label or chain[0].name,
        )


def _raise_exhausted(
    category: str, chain: list, last_exc: BaseException | None,
    last_failed: str | None, skipped: list[str],
) -> None:
    """链路全挂 —— 区分「全部被 breaker 跳过」与「全部尝试失败」给 log 更可读。"""
    chain_names = ",".join(p.name for p in chain)
    if last_exc is None:
        raise NoUpstreamAvailable(
            f"all {category} profiles unavailable (chain=[{chain_names}], "
            f"all in breaker cooling: {skipped})"
        )
    raise NoUpstreamAvailable(
        f"all {category} profiles failed (chain=[{chain_names}], "
        f"last_failed={last_failed}, last_error={last_exc!r})"
    )


async def run_with_fallback(
    category: str,
    chain: list,
    executor: Callable[[object], Awaitable[T]],
    *,
    primary_label: str = "",
) -> T:
    """按 chain 顺序尝试每条 profile（async）。

    Args:
        category: 模型类别，决定复合 breaker key 前缀。
        chain: [primary, fallback1, ...] 已 resolve 的 profile 列表。
        executor: per-profile 的异步执行回调，签名 ``(profile) -> Awaitable[T]``。
        primary_label: 日志里标识主 profile 的名字，缺省取 chain[0].name。

    Raises:
        NoUpstreamAvailable: 全部 profile 都不可用（breaker open 或失败）。
        其它异常: 首个遇到的不可前进错误（4xx 非 429 / permanent）原样上抛。
    """
    if not chain:
        raise NoUpstreamAvailable(f"empty {category} profile chain")
    last_exc: BaseException | None = None
    last_failed: str | None = None
    skipped: list[str] = []
    for idx, profile in enumerate(chain):
        if _should_skip_profile(category, profile, skipped):
            continue
        try:
            result = await executor(profile)
        except Exception as exc:
            if not _account_failure(category, profile, exc):
                raise
            last_exc, last_failed = exc, profile.name
            continue
        _on_success(category, idx, chain, profile, primary_label)
        return result
    _raise_exhausted(category, chain, last_exc, last_failed, skipped)
    raise AssertionError("unreachable")  # pragma: no cover - _raise_exhausted 必抛


def run_with_fallback_sync(
    category: str,
    chain: list,
    executor_sync: Callable[[object], T],
    *,
    primary_label: str = "",
) -> T:
    """``run_with_fallback`` 的同步镜像 —— worker subprocess 用。

    逻辑与 async 版完全一致，共用同一套 breaker 函数与同一份 ``_breakers``
    dict；唯一差别是 ``executor_sync`` 同步返回 ``T`` 而非 ``Awaitable[T]``。
    """
    if not chain:
        raise NoUpstreamAvailable(f"empty {category} profile chain")
    last_exc: BaseException | None = None
    last_failed: str | None = None
    skipped: list[str] = []
    for idx, profile in enumerate(chain):
        if _should_skip_profile(category, profile, skipped):
            continue
        try:
            result = executor_sync(profile)
        except Exception as exc:
            if not _account_failure(category, profile, exc):
                raise
            last_exc, last_failed = exc, profile.name
            continue
        _on_success(category, idx, chain, profile, primary_label)
        return result
    _raise_exhausted(category, chain, last_exc, last_failed, skipped)
    raise AssertionError("unreachable")  # pragma: no cover - _raise_exhausted 必抛
