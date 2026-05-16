#!/usr/bin/env python3
"""Reranker registry — 把 ProfileSet 中的 reranker profile 包装成
LightRAG 期望的 rerank_model_func。

设计与 embedding_registry.py 完全对称：
- `get_profile(name)` 字典式查询
- `build_rerank_func(profile, min_score)` 构造 (async callable, sig)
  sig = `rerank:{profile.name}`，加入 LightRAG cache key 让 reranker 切换强制重建
- module-level lazy singleton + clear_reranker_registry() 供测试 reset

Phase 2 范围：
- 只支持业界标准 OpenAI/Cohere-style POST /rerank schema
  （payload: {model, query, documents, top_n[, return_documents]}）
- 不做跨 reranker fallback（rerank 无 dim 一致性问题，留 Phase 3）
- min_score 在 callable 内做客户端过滤（与 LightRAG 全局 min_rerank_score 互补）

调用契约（与 lightrag/utils.py:apply_rerank_if_enabled 对齐）：
    rerank_func(query: str, documents: list[str], top_n: int|None)
        -> list[{"index": int, "relevance_score": float}]
索引指原始 documents 列表位置，由 LightRAG 自行映射回 retrieved_docs。
"""
from __future__ import annotations

import asyncio
import logging
import random
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable

import httpx

# 双重 import：worker subprocess（sys.path[0]=scripts）下 profiles_loader 是顶层；
# server 进程下 scripts/ 是 package，需带前缀。同模块两种入口都要支持。
try:
    from profiles_loader import (  # type: ignore[import-not-found]
        ProfileSet,
        RerankerProfile,
        load_profiles,
    )
except ImportError:  # pragma: no cover - package import path
    from scripts.profiles_loader import (  # type: ignore[import-not-found]
        ProfileSet,
        RerankerProfile,
        load_profiles,
    )

logger = logging.getLogger(__name__)

# 退避参数 — 与 embedding_registry 同款常量，保证两侧重试行为一致
_RETRY_BASE_DELAY_S = 1.5
_RETRY_MAX_DELAY_S = 30.0
_JITTER_RATIO = 0.25
_MIN_DELAY_S = 0.5

# rerank_func 签名 — 给类型注解用
RerankFunc = Callable[..., Awaitable[list[dict[str, Any]]]]


def _parse_retry_after(value: str | None) -> float | None:
    """解析 Retry-After 头：纯数字秒 或 HTTP-date 都支持。
    与 embedding_registry._parse_retry_after 一致，未复用是为了模块解耦
    （避免 rerank 链路在 embedding 模块缺失时挂掉）。
    """
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


def _rerank_backoff(attempt: int, retry_after: float | None) -> float:
    """指数退避 + 抖动 + 尊重 Retry-After（不超过 _RETRY_MAX_DELAY_S）。"""
    base = min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S)
    jitter = base * random.uniform(-_JITTER_RATIO, _JITTER_RATIO)
    delay = max(_MIN_DELAY_S, base + jitter)
    if retry_after is not None:
        delay = max(delay, min(retry_after, _RETRY_MAX_DELAY_S))
    return delay


# RerankerProfile 当前没有 max_retries 字段（与 EmbeddingProfile 不同），
# 用模块级常量保持默认值。后续若需 per-profile 可调，再加 dataclass 字段。
_DEFAULT_RERANK_MAX_RETRIES = 3


async def _http_rerank(
    profile: RerankerProfile,
    query: str,
    documents: list[str],
    top_n: int | None,
) -> list[tuple[int, float]]:
    """OpenAI/Cohere 兼容 /rerank 调用，带 429/5xx 重试 + Retry-After 解析。

    Returns:
        list of (index, relevance_score)。index 指原始 documents 顺序，
        外层调用方需自行映射回 retrieved_docs。

    Notes:
        - base_url 已含 `/v1`（按 profiles.yaml.example 约定，到 `/v1` 为止），
          故请求 URL 拼 `{base}/rerank`；self-hosted OpenAI-compatible gateway
          的 `text-reranker` / Cohere v2 / Jina v1 / vLLM rerank 均匹配此 schema。
        - return_documents=false：rerank 服务端只回 index+relevance_score，
          带宽和延迟最小化；我们不需要 server 回回原文。
        - 401/400 类错误不重试，直接抛给上层暴露配置/输入错误。
    """
    url = f"{profile.base_url.rstrip('/')}/rerank"
    headers = {"Authorization": f"Bearer {profile.api_key}"}
    payload: dict[str, Any] = {
        "model": profile.model,
        "query": query,
        "documents": documents,
        "return_documents": False,
    }
    if top_n is not None:
        payload["top_n"] = top_n

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(_DEFAULT_RERANK_MAX_RETRIES):
            retry_after: float | None = None
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    raise httpx.HTTPStatusError(
                        f"rerank upstream {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results")
                if not isinstance(results, list):
                    raise ValueError(
                        f"rerank response missing 'results' list: {str(data)[:200]!r}"
                    )
                # 按业界 schema 取 index + relevance_score；缺字段视为响应不合法
                parsed: list[tuple[int, float]] = []
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    try:
                        idx = int(item["index"])
                        score = float(item["relevance_score"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    parsed.append((idx, score))
                return parsed
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                # 4xx（非 429）配置/输入错误，不重试，直接暴露给上层
                if isinstance(exc, httpx.HTTPStatusError):
                    code = exc.response.status_code
                    if not (code == 429 or code >= 500):
                        raise
                last_exc = exc
                if attempt == _DEFAULT_RERANK_MAX_RETRIES - 1:
                    break
                delay = _rerank_backoff(attempt, retry_after)
                logger.warning(
                    "Rerank call failed for profile %s (attempt %d/%d): %s; retrying in %.1fs",
                    profile.name, attempt + 1, _DEFAULT_RERANK_MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


class RerankerRegistry:
    """ProfileSet 的 reranker 视图门面。

    线程安全：内部状态 frozen，方法纯函数。与 EmbeddingRegistry 平行存在，
    两者共享同一份 ProfileSet（由 load_profiles 统一返回）。
    """

    def __init__(self, profiles: ProfileSet) -> None:
        self._profiles = profiles

    @property
    def profiles(self) -> ProfileSet:
        return self._profiles

    def get_profile(self, name: str) -> RerankerProfile:
        """按名查 reranker profile，缺失抛 KeyError 暴露配置漏洞。"""
        try:
            return self._profiles.rerankers[name]
        except KeyError:
            raise KeyError(f"reranker profile {name!r} not found") from None

    def build_rerank_func(
        self,
        profile: RerankerProfile,
        min_score: float | None = None,
    ) -> tuple[RerankFunc, str]:
        """构造 LightRAG 期望的 rerank_model_func + cache signature。

        Args:
            profile: 已 resolve 的 reranker profile（caller 已做 get_profile）。
            min_score: 客户端侧过滤阈值；None 时退化到 profile.min_score。
                LightRAG 自身也有 min_rerank_score 字段，此处主要用于 sig 不变
                时让 build 调用方掌控阈值（不污染 module-level）。

        Returns:
            (async rerank_func, "rerank:{profile.name}")
            rerank_func 签名匹配 lightrag/utils.py:apply_rerank_if_enabled 调用：
                async (query, documents, top_n) -> list[{"index", "relevance_score"}]

        Behavior:
            - 空 documents 直接返回 []，不调 HTTP（与上游 LightRAG 早返一致）
            - relevance_score < min_score 的项过滤掉
            - 按 score 降序排序；top_n 截断在排序后做
            - HTTP 层错误透传给上层（LightRAG apply_rerank_if_enabled 会 catch 并降级）
        """
        threshold = profile.min_score if min_score is None else float(min_score)

        async def _rerank(
            query: str,
            documents: list[str],
            top_n: int | None = None,
            **_: Any,
        ) -> list[dict[str, Any]]:
            # 早返：documents 为空时不消耗一次网络 RTT
            if not documents:
                return []

            raw = await _http_rerank(profile, query, list(documents), top_n)

            # 客户端过滤 + 降序排序 + top_n 截断。三步分开是为了：
            # 1) 过滤优先，避免 top_n 把高分项挤掉再被 min_score 删
            # 2) 排序明确以 score 降序为权威，不依赖上游返回顺序
            # 3) top_n 在排序后截，与 Cohere/Jina/Aliyun 实测行为一致
            filtered = [(i, s) for i, s in raw if s >= threshold]
            filtered.sort(key=lambda kv: kv[1], reverse=True)
            if top_n is not None and len(filtered) > top_n:
                filtered = filtered[:top_n]

            # LightRAG apply_rerank_if_enabled 期望 dict-list；按其格式回传
            return [
                {"index": idx, "relevance_score": score}
                for idx, score in filtered
            ]

        sig = f"rerank:{profile.name}"
        return _rerank, sig


# ---- module-level lazy singleton ----------------------------------------
# 与 embedding_registry 同模式：第一次 get_reranker_registry() 触发 load_profiles，
# 之后所有调用方共享同一份 ProfileSet；测试可用 clear_reranker_registry() 重置。
_registry: RerankerRegistry | None = None


def get_reranker_registry() -> RerankerRegistry:
    """全局 RerankerRegistry 单例（lazy）。

    复用 load_profiles() — 与 EmbeddingRegistry 同源。两个 registry 各自缓存，
    切换 YAML 时调用方需同时 clear 两边（conftest 已处理）。
    """
    global _registry
    if _registry is None:
        _registry = RerankerRegistry(load_profiles())
    return _registry


def clear_reranker_registry() -> None:
    """测试专用：重置缓存，让下次 get_reranker_registry() 重新读 env / YAML。"""
    global _registry
    _registry = None
