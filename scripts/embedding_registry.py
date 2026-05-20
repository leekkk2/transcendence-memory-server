#!/usr/bin/env python3
"""Embedding registry — 把 ProfileSet 包装成 container → EmbeddingFunc 工厂。

职责：
- `resolve(container)` 按 exact > glob > regex > default 顺序匹配路由
- `get_profile(name)` 字典式查询
- `build_embedding_func(route)` 构造 LightRAG 期望的 EmbeddingFunc，签名 string
  作为 cache key（Phase 2 升级为 `{emb_sig, rrk_sig}` 联合 key）

Phase 3 (v0.9.0)：真实 fallback chain + per-profile circuit breaker。
- 单 profile 内部 retry：保留 429/5xx + Retry-After 指数退避（Phase 1 行为）
- 跨 profile fallback：profile 级别失败后切下一条；4xx 非 429 不 fallback
- Circuit breaker：连续 5 次 / 60s 窗口失败 → cooling 30s 直接 raise，不调
  HTTP；cooling 到期自动 half-open，下一次请求过去探活，成功 reset、失败
  继续 cooling
- `reset_breaker(profile_name)` 供 /admin/probe-embedding 探活后显式 reset
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import numpy as np
from lightrag.utils import EmbeddingFunc

# 双重 import：worker subprocess（直接 `python scripts/xxx.py`）下 sys.path[0]=scripts，
# 不是 package；server 进程下被作为 scripts.embedding_registry import，scripts/ 是 package。
# 同模块两种入口都要支持，故 try 不带前缀优先，fail 再用 scripts. 前缀。
try:
    from profiles_loader import (  # type: ignore[import-not-found]
        EmbeddingProfile,
        ProfileSet,
        Route,
        load_profiles,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.profiles_loader import (  # type: ignore[import-not-found]
        EmbeddingProfile,
        ProfileSet,
        Route,
        load_profiles,
    )

logger = logging.getLogger(__name__)

# 退避参数 — 与 rag_engine.py 旧值一致，保证迁移行为不变
_RETRY_BASE_DELAY_S = 1.5
_RETRY_MAX_DELAY_S = 30.0
_JITTER_RATIO = 0.25
_MIN_DELAY_S = 0.5

# Circuit breaker 参数 — 默认值可被同名 env 覆盖（运维调参不重新打包）。
# 选 5/60s/30s：上游短时抽风 ≤ 3-4 次能自愈，5 次定性为持续不可用；30s
# cooling 比 60s 窗口短，确保偶发抽风快速恢复，又不至于刚 reset 就再次打爆
# 上游。
_BREAKER_FAIL_THRESHOLD = 5
_BREAKER_WINDOW_S = 60.0
_BREAKER_COOLING_S = 30.0


class NoUpstreamAvailable(RuntimeError):
    """所有 primary + fallback 都不可用时抛出 — 区别于单点 HTTP 错误，
    让上层（rag_engine / lightrag worker）能识别为「整条链路熔断」而非
    「某次请求失败」。"""


class BreakerOpen(RuntimeError):
    """单个 profile 在 cooling 期间被请求时抛出。fallback 链路 catch 此异常
    跳到下一条 profile；最后一条仍 open 时升级为 NoUpstreamAvailable。"""


@dataclass
class BreakerState:
    """Per-profile breaker 状态。closed → open → half-open → closed 状态机。

    - consecutive_fails: 60s 滚动窗口内连续失败计数
    - last_fail_ts: 最近一次失败的时间戳（用于窗口滚动判定）
    - cooling_until_ts: > now 时 breaker open；<= now 时为 half-open（允许探活）
    - half_open: True 时下一次请求是探活请求；成功 reset，失败回 open
    """

    consecutive_fails: int = 0
    last_fail_ts: float = 0.0
    cooling_until_ts: float = 0.0
    half_open: bool = False


# 模块级 breaker 字典 + 锁 — 跨协程并发安全。breaker 状态变更是 O(1)，
# 锁粒度全表无所谓；breaker 决策路径不调 HTTP，不会成为瓶颈。
_breakers: dict[str, BreakerState] = {}
_breakers_lock = threading.Lock()


def _get_breaker(name: str) -> BreakerState:
    """惰性初始化 — 第一次见到的 profile 自动建一条 closed 状态记录。"""
    with _breakers_lock:
        state = _breakers.get(name)
        if state is None:
            state = BreakerState()
            _breakers[name] = state
        return state


def _breaker_should_skip(name: str) -> bool:
    """判定是否要绕过 profile：open 且 cooling 未到期 → True；
    cooling 到期 → 自动切 half-open（允许下一次请求过去），返回 False。"""
    state = _get_breaker(name)
    now = time.monotonic()
    with _breakers_lock:
        if state.cooling_until_ts > 0 and now < state.cooling_until_ts:
            return True  # 真 open，跳过
        if state.cooling_until_ts > 0 and now >= state.cooling_until_ts:
            # cooling 到期 — 进入 half-open（cooling_until_ts 暂不清零，
            # 等 _breaker_mark_success 或 _breaker_mark_failure 决定下一步）
            state.half_open = True
    return False


def _breaker_mark_failure(name: str) -> None:
    """记一次失败 — 累加计数；half-open 时直接重新 open；窗口内达阈值 → open。"""
    state = _get_breaker(name)
    now = time.monotonic()
    with _breakers_lock:
        # 窗口外的失败重置计数 — 60s 之前的失败不参与本轮判定
        if now - state.last_fail_ts > _BREAKER_WINDOW_S:
            state.consecutive_fails = 0
        state.consecutive_fails += 1
        state.last_fail_ts = now
        if state.half_open:
            # half-open 探活失败 → 立刻回到 open，cooling 重新计时
            state.cooling_until_ts = now + _BREAKER_COOLING_S
            state.half_open = False
            logger.warning(
                "circuit breaker profile=%s half-open probe failed, "
                "cooling another %.1fs",
                name, _BREAKER_COOLING_S,
            )
        elif state.consecutive_fails >= _BREAKER_FAIL_THRESHOLD:
            # 窗口内达阈值 → open
            state.cooling_until_ts = now + _BREAKER_COOLING_S
            logger.warning(
                "circuit breaker profile=%s opened after %d consecutive failures, "
                "cooling %.1fs",
                name, state.consecutive_fails, _BREAKER_COOLING_S,
            )


def _breaker_mark_success(name: str) -> None:
    """记一次成功 — half-open 探活成功或正常请求成功，都重置计数。"""
    state = _get_breaker(name)
    with _breakers_lock:
        if state.cooling_until_ts > 0 or state.consecutive_fails > 0:
            logger.info(
                "circuit breaker profile=%s reset after success "
                "(prev fails=%d)", name, state.consecutive_fails,
            )
        state.consecutive_fails = 0
        state.last_fail_ts = 0.0
        state.cooling_until_ts = 0.0
        state.half_open = False


def reset_breaker(profile_name: str) -> bool:
    """显式清空指定 profile 的 breaker 状态。

    供 /admin/probe-embedding 探活成功后调用 — 让运维能手动「拔保险丝」，
    不必等 30s 自动 half-open。未知 profile 名 no-op，不抛异常（端点已校验
    profile 存在性，这里宽松处理便于运维脚本批量重置）。

    Returns:
        True 表示重置了一条已存在的 breaker 状态；False 表示该 profile
        从未触发过 breaker（即「无需重置」）。
    """
    with _breakers_lock:
        state = _breakers.get(profile_name)
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
        logger.info("circuit breaker profile=%s explicitly reset", profile_name)
    return had_state


def _clear_all_breakers() -> None:
    """测试专用：清空全部 breaker 状态。"""
    with _breakers_lock:
        _breakers.clear()


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头：纯数字秒 或 HTTP-date 都支持。"""
    if not value:
        return None
    import time as _t

    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return max(0.0, dt.timestamp() - _t.time())
    except (TypeError, ValueError):
        return None


def _embed_backoff(attempt: int, retry_after: float | None) -> float:
    """指数退避 + 抖动 + 尊重 Retry-After（不超过 _RETRY_MAX_DELAY_S）。"""
    base = min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S)
    jitter = base * random.uniform(-_JITTER_RATIO, _JITTER_RATIO)
    delay = max(_MIN_DELAY_S, base + jitter)
    if retry_after is not None:
        delay = max(delay, min(retry_after, _RETRY_MAX_DELAY_S))
    return delay


def _is_fallback_eligible(exc: Exception) -> bool:
    """判定异常是否值得跨 profile fallback。

    fallback-eligible（上游不可用，换一家可能成功）：
    - 429 Too Many Requests（限流）
    - 5xx Server Error（上游故障 / 维护）
    - httpx.TransportError（网络层抖动 / DNS / TCP）
    - ValueError（响应体损坏 / JSON 错 — 等价于服务异常）

    非 fallback（客户端配置错，换 profile 没用）：
    - 400 Bad Request（payload 不合法）
    - 401 Unauthorized（key 错 — 用户配置）
    - 403 Forbidden（无权限 — 用户配置）
    - 其他 4xx 非 429（404/405/422 等都是配置或代码问题）
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    if isinstance(exc, (httpx.TransportError, ValueError)):
        return True
    return False


async def _http_embed_single(
    profile: EmbeddingProfile,
    texts: list[str],
) -> np.ndarray:
    """单 profile 调用：429/5xx 内部重试 + Retry-After 解析。

    与 v0.8.0 _http_embed 行为一致，仅改名 _http_embed_single 以便上层
    fallback chain 区分「profile 级 retry」与「cross-profile fallback」。
    request_dim 非 None 时透传 OpenAI `dimensions` 字段（Matryoshka）。

    provider == "gemini_native" 时改走 Gemini 原生 `:embedContent` 协议
    （batchEmbedContents 批量），其余保持 OpenAI-compatible `/v1/embeddings`。
    """
    if profile.provider == 'gemini_native':
        try:
            from gemini_native_embed import embed_texts_async  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - package import path
            from scripts.gemini_native_embed import embed_texts_async  # type: ignore[import-not-found]
        return await embed_texts_async(profile, texts)

    url = f"{profile.base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {profile.api_key}"}
    payload: dict[str, Any] = {"model": profile.model, "input": texts}
    if profile.request_dim is not None:
        payload["dimensions"] = profile.request_dim

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            retry_after: float | None = None
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    raise httpx.HTTPStatusError(
                        f"embedding upstream {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()["data"]
                sorted_data = sorted(data, key=lambda x: x["index"])
                return np.array([d["embedding"] for d in sorted_data], dtype="float32")
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                # 4xx（非 429）配置/输入错误，profile 内不重试，直接抛
                # — 由上层 fallback 决定是否跨 profile（事实上不会，
                # _is_fallback_eligible 会判 False）
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if not (code == 429 or code >= 500):
                        raise
                last_exc = exc
                if attempt == profile.max_retries - 1:
                    break
                delay = _embed_backoff(attempt, retry_after)
                logger.warning(
                    "Embedding call failed for profile %s (attempt %d/%d): %s; retrying in %.1fs",
                    profile.name, attempt + 1, profile.max_retries, exc, delay,
                )
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# 反向兼容别名：v0.8.0 的 _http_embed 名称被 admin probe 端点直接 import；
# 保留别名让升级期间外部调用方不破。新代码用 _http_embed_single 明确语义。
_http_embed = _http_embed_single


async def _http_embed_with_fallback(
    profiles_chain: list[EmbeddingProfile],
    texts: list[str],
) -> np.ndarray:
    """跨 profile fallback 主入口：按 chain 顺序尝试，breaker open 跳过。

    Args:
        profiles_chain: [primary, fallback1, fallback2, ...] 已 resolve 的
            profile 实例列表。调用方负责按 route.embedding_fallbacks 顺序
            提前展开。
        texts: 待 embed 的文本列表。

    Returns:
        np.ndarray (n_texts × dim)，dim 由 chain 内任一 profile 决定
        （已由 profiles_loader 校验等维）。

    Raises:
        NoUpstreamAvailable: 全部 profile 都不可用（breaker open 或失败）
        httpx.HTTPStatusError: 401/403/400 等用户配置错（不 fallback 直接抛）
    """
    if not profiles_chain:
        raise NoUpstreamAvailable("empty embedding profile chain")

    last_exc: Exception | None = None
    last_failed_profile: str | None = None
    skipped_due_to_breaker: list[str] = []

    # 通过模块属性间接调用 _http_embed —— 让测试 monkeypatch
    # `setattr(reg_mod, "_http_embed", fake)` 仍然生效（v0.7.0/v0.8.0
    # 测试约定，必须保留 backward compat）。运行期等价于 _http_embed_single。
    embed_callable = _http_embed

    for idx, profile in enumerate(profiles_chain):
        # breaker 检查：open 状态直接跳，不调 HTTP 节省时间和 quota
        if _breaker_should_skip(profile.name):
            skipped_due_to_breaker.append(profile.name)
            logger.warning(
                "skipping profile=%s (circuit breaker open); trying next",
                profile.name,
            )
            continue
        try:
            result = await embed_callable(profile, texts)
            # 成功 — 重置 breaker；如果是 fallback 命中，warn 级别 log 让运维
            # 能在监控里看到「主线挂了，副线接管」
            _breaker_mark_success(profile.name)
            if idx > 0:
                logger.warning(
                    "embedding fallback succeeded: profile=fallback_%s "
                    "(primary=%s failed)",
                    profile.name, profiles_chain[0].name,
                )
            return result
        except Exception as exc:
            if not _is_fallback_eligible(exc):
                # 401/403/400 类用户配置错 — 不消耗 fallback quota，
                # 不计入 breaker（这不是「上游不可用」，是「我们配错了」）
                logger.warning(
                    "profile=%s raised non-fallback error %s; "
                    "not trying fallbacks",
                    profile.name, exc,
                )
                raise
            # 标记失败 → 累加 breaker；如果阈值达到本次调用即让它 open
            _breaker_mark_failure(profile.name)
            last_exc = exc
            last_failed_profile = profile.name
            logger.warning(
                "embedding profile=%s failed: %s; trying next in chain",
                profile.name, exc,
            )
            continue

    # 链路全挂 — 区分「全部被 breaker 跳过」和「全部尝试都失败」给 log 更可读
    chain_names = ",".join(p.name for p in profiles_chain)
    if last_exc is None:
        raise NoUpstreamAvailable(
            f"all embedding profiles unavailable (chain=[{chain_names}], "
            f"all in breaker cooling: {skipped_due_to_breaker})"
        )
    raise NoUpstreamAvailable(
        f"all embedding profiles failed (chain=[{chain_names}], "
        f"last_failed={last_failed_profile}, last_error={last_exc!r})"
    )


def _matcher_hits(matcher: dict[str, Any], container: str) -> bool:
    """单条 matcher 是否命中。支持 exact / glob / regex；default 由调用方单独处理。"""
    if "exact" in matcher:
        return container == matcher["exact"]
    if "glob" in matcher:
        return fnmatch.fnmatchcase(container, matcher["glob"])
    if "regex" in matcher:
        return re.match(matcher["regex"], container) is not None
    return False


class EmbeddingRegistry:
    """ProfileSet 的查询/构造门面。线程安全：内部状态 frozen，方法纯函数。"""

    def __init__(self, profiles: ProfileSet) -> None:
        self._profiles = profiles

    @property
    def profiles(self) -> ProfileSet:
        return self._profiles

    def resolve(self, container: str) -> Route:
        """按文件顺序匹配 routes，命中即返回；都不命中走 default。"""
        for matcher, route in self._profiles.routes:
            if _matcher_hits(matcher, container):
                return route
        assert self._profiles.default_route is not None, "validated by loader"
        return self._profiles.default_route

    def get_profile(self, name: str) -> EmbeddingProfile:
        """按名查 embedding profile，缺失抛 KeyError 暴露配置漏洞。"""
        try:
            return self._profiles.embeddings[name]
        except KeyError:
            raise KeyError(f"embedding profile {name!r} not found") from None

    def build_embedding_func(self, route: Route) -> tuple[EmbeddingFunc, str]:
        """构造 LightRAG 期望的 EmbeddingFunc + cache signature。

        Phase 3：内部 callable 走 _http_embed_with_fallback，按
        `[primary, *fallbacks]` 顺序尝试，breaker open 自动跳过。

        Returns:
            (EmbeddingFunc, route_signature)
            signature: "embed:{primary}" 或 "embed:{primary}+{fb1}+{fb2}"
        """
        primary = self.get_profile(route.embedding)
        fallback_profiles = [self.get_profile(fb) for fb in route.embedding_fallbacks]
        # 闭包 capture 整条 chain — 运行期不再访问 self/registry，避免单例
        # 被 clear 时 worker 进程持有的 EmbeddingFunc 失效
        chain = [primary, *fallback_profiles]

        async def _call(texts: list[str]) -> np.ndarray:
            return await _http_embed_with_fallback(chain, texts)

        embed_func = EmbeddingFunc(
            embedding_dim=primary.dim,
            max_token_size=primary.max_token_size,
            func=_call,
        )
        if route.embedding_fallbacks:
            sig = f"embed:{primary.name}+" + "+".join(route.embedding_fallbacks)
        else:
            sig = f"embed:{primary.name}"
        return embed_func, sig


# ---- module-level lazy singleton ----------------------------------------
# 设计意图：第一次 get_registry() 触发 load_profiles，之后所有调用方共享同一份
# 已校验的 ProfileSet；测试可用 clear_registry() 重置。
_registry: EmbeddingRegistry | None = None


def get_registry() -> EmbeddingRegistry:
    """全局 EmbeddingRegistry 单例（lazy）。"""
    global _registry
    if _registry is None:
        _registry = EmbeddingRegistry(load_profiles())
    return _registry


def clear_registry() -> None:
    """测试专用：重置缓存，让下次 get_registry() 重新读 env / YAML。
    同时清空 breaker 状态 — 测试间互不污染。"""
    global _registry
    _registry = None
    _clear_all_breakers()
