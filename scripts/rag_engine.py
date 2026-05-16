#!/usr/bin/env python3
"""LightRAG engine module — manages per-container LightRAG instances.

纯文本入库与查询直接使用 LightRAG，不依赖 RAGAnything 的多模态 parser。
多模态（PDF/图像/表格）场景若有需要，应在独立入口中显式加载 RAGAnything，
且该入口必须保证 parser 已正确安装。

v0.7.0 multi-embedding 升级（2026-05-16）：
  - 删除 module-level 全局 BASE_URL/API_KEY/EMBEDDING_MODEL/EMBEDDING_DIM
    以及 _embed_func。embedding 逻辑迁移到 scripts/embedding_registry.py，
    由 EmbeddingRegistry 按 container -> route 动态解析与构造 EmbeddingFunc。
  - get_lightrag(container) cache key 升级为 (container, route_sig)，
    避免在 route / profile 切换后命中过期 LightRAG instance（embedding_dim
    可能不同，复用旧 instance 会导致 LanceDB 向量维度 mismatch）。
  - LLM 调用保持不变（与 embedding 解耦），call_openai_chat / _llm_func 保留。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# LLM 仍由 env 直接驱动（multi-LLM 升级是后续 phase，本次只动 embedding）。
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
# LLM_BASE_URL / LLM_API_KEY 历史上 fallback 到 EMBEDDING_BASE_URL / EMBEDDING_API_KEY；
# 保留 fallback 以避免现有 .env 配置（很多部署只配了一对 base/key）失效。
LLM_BASE_URL = (
    os.environ.get("LLM_BASE_URL")
    or os.environ.get("EMBEDDING_BASE_URL")
    or os.environ.get("EMBEDDINGS_BASE_URL")
    or "https://api.openai.com/v1"
)
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("EMBEDDING_API_KEY", "")
WS = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
RAG_SEARCH_MODE = os.environ.get("RAG_SEARCH_MODE", "hybrid")

# cache key = (container, emb_sig, rrk_sig)；两 sig 都由 registry 给出。
#   emb_sig: "embed:gemini-3072" 或 "embed:primary+fb1+fb2"
#   rrk_sig: "rerank:cohere-v3" 或 "" 当 route 未配置 reranker
# Phase 2：rrk_sig 进 cache key 让 reranker 切换强制重建 LightRAG instance，避免
# 复用旧 rerank_model_func 闭包导致行为不一致。
_lightrag_instances: dict[tuple[str, str, str], Any] = {}
_lightrag_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


_LLM_MAX_RETRIES = int(os.environ.get("LLM_MAX_RETRIES", "4"))
_LLM_RETRY_BASE_DELAY = float(os.environ.get("LLM_RETRY_BASE_DELAY", "1.5"))


async def call_openai_chat(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    timeout: float = 180.0,
    label: str = "LLM",
) -> str:
    """OpenAI 兼容 chat/completions 调用，带指数退避重试。

    - 5xx / 连接错误 / 空 content / JSON 解析失败都视为可重试
    - 429 也纳入重试（不尊重 Retry-After，简单指数退避）
    - 最终失败透传原始异常
    """
    import httpx

    if not base_url:
        raise RuntimeError(f"{label} base_url is empty; configure LLM_BASE_URL/VLM_BASE_URL")

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": messages}
    headers = {"Authorization": f"Bearer {api_key}"}

    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(_LLM_MAX_RETRIES):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise httpx.HTTPStatusError(
                        f"upstream {resp.status_code}", request=resp.request, response=resp,
                    )
                resp.raise_for_status()
                try:
                    data = resp.json()
                except ValueError as json_err:
                    raise ValueError(
                        f"{label} returned non-JSON body: {resp.text[:200]!r}"
                    ) from json_err
                content = data["choices"][0]["message"].get("content")
                if not content:
                    raise ValueError(f"{label} returned empty content: {str(data)[:300]}")
                return content
            except (httpx.HTTPStatusError, httpx.TransportError, ValueError) as exc:
                last_exc = exc
                if attempt == _LLM_MAX_RETRIES - 1:
                    break
                delay = _LLM_RETRY_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    "%s call failed (attempt %d/%d): %s; retrying in %.1fs",
                    label, attempt + 1, _LLM_MAX_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


async def _llm_func(prompt: str, system_prompt: str | None = None, **_: Any) -> str:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return await call_openai_chat(
        base_url=LLM_BASE_URL,
        api_key=LLM_API_KEY,
        model=LLM_MODEL,
        messages=messages,
        label="LLM",
    )


def _container_working_dir(container: str) -> Path:
    path = WS / "tasks" / "rag" / "containers" / container / "raganything"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _get_lock(container: str) -> asyncio.Lock:
    async with _global_lock:
        if container not in _lightrag_locks:
            _lightrag_locks[container] = asyncio.Lock()
        return _lightrag_locks[container]


def _resolve_route_emb_rrk(
    container: str,
) -> tuple[Any, Any, str, Any, str, float | None]:
    """通过 registry 解析 container -> route -> (embedding_func, rerank_func)。

    Phase 2 升级版（取代旧 _resolve_route_and_embedding）：同时解析 reranker
    并返回构造好的 rerank_func + signature + min_score。如果 route 未配置
    reranker，rerank_func 为 None、rrk_sig 为 ""。

    Returns:
        (route, embedding_func, emb_sig, rerank_func, rrk_sig, min_score)
        - rerank_func: None 或 async callable（lightrag rerank_model_func 兼容签名）
        - rrk_sig: "" 当无 reranker，否则 "rerank:{profile_name}"
        - min_score: None 或 float，profile.min_score（注入 LightRAG min_rerank_score）
    """
    # 延迟 import：embedding_registry 由 v0.7.0 agent-α 实现；当 module-level
    # import 失败（例如本地开发未提供 profiles.yaml）时直接报错而非启动时挂掉。
    try:
        from .embedding_registry import get_registry
    except ImportError:  # pragma: no cover - 兼容 sys.path 注入 scripts/ 的部署
        from embedding_registry import get_registry  # type: ignore

    registry = get_registry()
    route = registry.resolve(container)
    embedding_func, emb_sig = registry.build_embedding_func(route)

    rerank_func: Any = None
    rrk_sig = ""
    min_score: float | None = None
    if route.reranker is not None:
        try:
            from .reranker_registry import get_reranker_registry
        except ImportError:  # pragma: no cover - 兼容 sys.path 注入 scripts/ 的部署
            from reranker_registry import get_reranker_registry  # type: ignore
        rrk_registry = get_reranker_registry()
        rrk_profile = rrk_registry.get_profile(route.reranker)
        rerank_func, rrk_sig = rrk_registry.build_rerank_func(rrk_profile)
        min_score = rrk_profile.min_score

    return route, embedding_func, emb_sig, rerank_func, rrk_sig, min_score


# v0.7.0 旧 helper 名称向后兼容（其他模块/测试可能持有引用）。
# Phase 2 内部统一用 _resolve_route_emb_rrk，旧函数仅作为薄壳。
def _resolve_route_and_embedding(container: str) -> tuple[Any, str, Any]:
    route, embedding_func, emb_sig, _rrk, _rrk_sig, _min = _resolve_route_emb_rrk(container)
    return route, emb_sig, embedding_func


async def get_lightrag(container: str) -> Any:
    """获取（必要时创建并初始化）container 对应的 LightRAG 实例。

    cache key 升级为 (container, emb_sig, rrk_sig)：
      - container 切换 route（如 yaml hot-reload 后路由变更）时不复用旧 instance
      - emb_sig 含 embedding profile 名 + dim，dim 变化必触发新建
      - rrk_sig 含 reranker profile 名（或 "" 表示无 reranker）；reranker 切换
        强制新建 instance，避免复用旧 rerank_model_func 闭包
    """
    route, embedding_func, emb_sig, rerank_func, rrk_sig, min_score = \
        _resolve_route_emb_rrk(container)
    cache_key = (container, emb_sig, rrk_sig)

    instance = _lightrag_instances.get(cache_key)
    if instance is not None:
        return instance

    lock = await _get_lock(container)
    async with lock:
        instance = _lightrag_instances.get(cache_key)
        if instance is not None:
            return instance

        from lightrag import LightRAG
        from lightrag.kg.shared_storage import initialize_pipeline_status

        working_dir = _container_working_dir(container)
        working_dir.mkdir(parents=True, exist_ok=True)

        # 只在 route 实际配置 reranker 时才传 rerank_model_func / min_rerank_score；
        # 否则保持 v0.7.0 行为完全等价 — 不动 LightRAG 默认值。
        lightrag_kwargs: dict[str, Any] = dict(
            working_dir=str(working_dir),
            llm_model_func=_llm_func,
            embedding_func=embedding_func,
        )
        if rerank_func is not None:
            lightrag_kwargs["rerank_model_func"] = rerank_func
            if min_score is not None:
                lightrag_kwargs["min_rerank_score"] = min_score

        instance = LightRAG(**lightrag_kwargs)
        await instance.initialize_storages()
        await initialize_pipeline_status()

        _lightrag_instances[cache_key] = instance
        logger.info(
            "LightRAG instance initialized for container=%s emb=%s rrk=%s at %s",
            container, emb_sig, rrk_sig or "<none>", working_dir,
        )
        return instance


def clear_rag_cache() -> None:
    """Test helper — also clears registries so reload picks up new env / yaml."""
    _lightrag_instances.clear()
    _lightrag_locks.clear()
    # 同步清 registry：测试切换 env / profiles.yaml 后必须让 registry 重新加载，
    # 否则 build_*_func 仍返回旧 profile 对应的闭包。embedding 与 reranker
    # 各自缓存 ProfileSet，两边都要清。
    try:
        from .embedding_registry import clear_registry
    except ImportError:  # pragma: no cover
        from embedding_registry import clear_registry  # type: ignore
    clear_registry()
    try:
        from .reranker_registry import clear_reranker_registry
    except ImportError:  # pragma: no cover
        from reranker_registry import clear_reranker_registry  # type: ignore
    clear_reranker_registry()
