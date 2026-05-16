#!/usr/bin/env python3
"""Embedding registry — 把 ProfileSet 包装成 container → EmbeddingFunc 工厂。

职责：
- `resolve(container)` 按 exact > glob > regex > default 顺序匹配路由
- `get_profile(name)` 字典式查询
- `build_embedding_func(route)` 构造 LightRAG 期望的 EmbeddingFunc，签名 string
  作为 cache key（Phase 2 升级为 `{emb_sig, rrk_sig}` 联合 key）

Phase 1 范围：fallback 信息只进 signature 字符串，真实切换逻辑留到 Phase 3
（_http_embed 现阶段只调 primary，sustained 429/quota 直接抛给上层）。
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import random
import re
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


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头：纯数字秒 或 HTTP-date 都支持。"""
    if not value:
        return None
    import time

    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return max(0.0, dt.timestamp() - time.time())
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


async def _http_embed(profile: EmbeddingProfile, texts: list[str]) -> np.ndarray:
    """OpenAI 兼容 /embeddings 调用，带 429/5xx 重试 + Retry-After 解析。

    从 rag_engine.py 的 _embed_func 迁移而来，把模块全局 BASE_URL/API_KEY/MODEL
    换成 EmbeddingProfile 参数，让每条 profile 都能独立调度。`request_dim` 非 None
    时透传为 OpenAI `dimensions` 字段，支持 Matryoshka embedding 截断。
    """
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
                # 4xx（非 429）配置/输入错误，不重试，直接暴露给上层
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

        Phase 1：只把 primary profile 实际接通；fallback chain 结构化进 signature
        以便 Phase 3 切换时 cache key 自然变化、强制重建 LightRAG instance。

        Returns:
            (EmbeddingFunc, route_signature)
            signature: "embed:{primary}" 或 "embed:{primary}+{fb1}+{fb2}"
        """
        primary = self.get_profile(route.embedding)
        for fb in route.embedding_fallbacks:  # 触发缺失检查，启动期暴露
            self.get_profile(fb)

        async def _call(texts: list[str]) -> np.ndarray:
            # 闭包 capture primary，避免依赖 module-level 状态
            return await _http_embed(primary, texts)

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
    """测试专用：重置缓存，让下次 get_registry() 重新读 env / YAML。"""
    global _registry
    _registry = None
