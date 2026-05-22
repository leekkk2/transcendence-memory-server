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
import sys
# time 仍 import：测试通过 `monkeypatch.setattr(embedding_registry.time, ...)`
# 控制虚拟时钟，需 time 模块在本模块命名空间可达（与 model_fallback 共用同一
# stdlib 模块对象）。
import time  # noqa: F401
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

# 双重 import + 模块身份归一。worker subprocess（bare import）与 server/package
# （scripts. 前缀）两种入口可能各加载一份 embedding_errors 副本 —— typed
# exception 的 isinstance 跨副本会失效。这里复用任一已加载副本，并登记到两个
# 模块名下，保证全进程只有一份类对象。
embedding_errors = (
    sys.modules.get('scripts.embedding_errors')
    or sys.modules.get('embedding_errors')
)
if embedding_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import embedding_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import embedding_errors  # type: ignore[no-redef]
sys.modules.setdefault('embedding_errors', embedding_errors)
sys.modules.setdefault('scripts.embedding_errors', embedding_errors)

logger = logging.getLogger(__name__)

# 退避参数 — 与 rag_engine.py 旧值一致，保证迁移行为不变
_RETRY_BASE_DELAY_S = 1.5
_RETRY_MAX_DELAY_S = 30.0
_JITTER_RATIO = 0.25
_MIN_DELAY_S = 0.5

# 通用 fallback 核心 — 熔断器 + 泛型 runner 已抽到 scripts/model_fallback.py，
# 本模块改为它的消费者。沿用 embedding_errors 同款双重 import + 模块身份归一：
# worker subprocess（bare import）与 server 进程（package import）必须共用同一份
# breaker dict 与同一份类对象，否则跨副本 isinstance / breaker 计数会失效。
model_fallback = (
    sys.modules.get('scripts.model_fallback')
    or sys.modules.get('model_fallback')
)
if model_fallback is None:  # pragma: no cover - 取决于运行入口
    try:
        import model_fallback  # type: ignore[no-redef]
    except ImportError:
        from scripts import model_fallback  # type: ignore[no-redef]
sys.modules.setdefault('model_fallback', model_fallback)
sys.modules.setdefault('scripts.model_fallback', model_fallback)

# re-export：保旧 import 路径不破 — 外部调用方与测试直接
# `from scripts.embedding_registry import NoUpstreamAvailable / BreakerOpen / ...`。
NoUpstreamAvailable = model_fallback.NoUpstreamAvailable
BreakerOpen = model_fallback.BreakerOpen
BreakerState = model_fallback.BreakerState
_is_fallback_eligible = model_fallback._is_fallback_eligible
_clear_all_breakers = model_fallback._clear_all_breakers
# _breakers 是同一份 dict 对象引用 — 测试直读 embedding_registry._breakers 仍可。
# 注意：key 现为复合 {category}:{profile}，embedding 链路统一前缀 "embed:"。
_breakers = model_fallback._breakers
_BREAKER_FAIL_THRESHOLD = model_fallback._BREAKER_FAIL_THRESHOLD
_BREAKER_WINDOW_S = model_fallback._BREAKER_WINDOW_S
_BREAKER_COOLING_S = model_fallback._BREAKER_COOLING_S

# embedding 链路的 breaker category 前缀 — 复合 key {category}:{profile} 让
# 同名 profile 在 embed / llm / vlm / rerank 各类别下互不串号。
_EMBED_CATEGORY = "embed"


def reset_breaker(profile_name: str) -> bool:
    """显式清空指定 embedding profile 的 breaker 状态。

    保留 v0.9.0 单参数签名（/admin/probe-embedding 探活端点直接调用）；内部
    转发到通用核心，category 固定为 embed。

    Returns:
        True 表示重置了一条已存在的非空 breaker 状态；False 表示该 profile
        从未触发过 breaker（即「无需重置」）。
    """
    return model_fallback.reset_breaker(_EMBED_CATEGORY, profile_name)


def _breaker_mark_failure(profile_name: str) -> None:
    """embedding profile 的 breaker 失败计数 —— 转发到通用核心（category=embed）。

    保留单参数签名供 /admin/probe-embedding 探活失败时累加计数；内部补上
    embed category 前缀构造复合 key。
    """
    model_fallback._breaker_mark_failure(
        model_fallback._breaker_key(_EMBED_CATEGORY, profile_name)
    )


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
    # 重试耗尽后用这三项把失败归类成 typed exception（embedding_errors）。
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
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
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if not (code == 429 or code >= 500):
                        # 4xx（非 429）配置/输入错误，profile 内不重试，原样抛
                        # httpx.HTTPStatusError —— 上层 _is_fallback_eligible
                        # 判 False，不跨 profile fallback。
                        raise
                    last_status = code
                    last_retry_after = retry_after
                    last_error_class = None
                elif isinstance(exc, httpx.TimeoutException):
                    last_status = None
                    last_retry_after = None
                    last_error_class = embedding_errors.TIMEOUT
                else:
                    # httpx.TransportError（非超时）/ ValueError → transient
                    last_status = None
                    last_retry_after = None
                    last_error_class = None
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
    # 重试耗尽 —— 归一为 typed exception，让 backlog 调度按 error_class 决策。
    raise embedding_errors.make_embedding_error(
        f"embedding upstream failed after {profile.max_retries} retries "
        f"(profile={profile.name}): {last_exc}",
        status=last_status,
        retry_after=last_retry_after,
        error_class=last_error_class,
    )


# 反向兼容别名：v0.8.0 的 _http_embed 名称被 admin probe 端点直接 import；
# 保留别名让升级期间外部调用方不破。新代码用 _http_embed_single 明确语义。
_http_embed = _http_embed_single


async def _http_embed_with_fallback(
    profiles_chain: list[EmbeddingProfile],
    texts: list[str],
) -> np.ndarray:
    """跨 profile fallback 主入口 —— 委托给通用 run_with_fallback。

    行为与 v0.9.0 内嵌实现等价（回归测试保证）：按 chain 顺序尝试，breaker
    open 跳过，4xx 非 429 / permanent / context-overflow 直接上抛，全挂抛
    NoUpstreamAvailable。

    Args:
        profiles_chain: [primary, fallback1, ...] 已 resolve 的 profile 列表。
            调用方负责按 route.embedding_fallbacks 顺序提前展开。
        texts: 待 embed 的文本列表。

    Returns:
        np.ndarray (n_texts × dim)，dim 由 chain 内任一 profile 决定
        （已由 profiles_loader 校验等维）。

    Raises:
        NoUpstreamAvailable: 全部 profile 都不可用（breaker open 或失败）。
        httpx.HTTPStatusError: 401/403/400 等用户配置错（不 fallback 直接抛）。
    """

    # executor 在调用时才读模块属性 _http_embed —— 让测试 monkeypatch
    # `setattr(reg_mod, "_http_embed", fake)` 仍然生效（v0.7.0/v0.8.0 测试
    # 约定，必须保留 backward compat）。运行期等价于 _http_embed_single。
    async def _executor(profile: EmbeddingProfile) -> np.ndarray:
        return await _http_embed(profile, texts)

    return await model_fallback.run_with_fallback(
        _EMBED_CATEGORY, profiles_chain, _executor,
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
