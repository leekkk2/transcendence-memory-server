#!/usr/bin/env python3
"""LightRAG engine module — manages per-container LightRAG instances.

纯文本入库与查询直接使用 LightRAG，不依赖 RAGAnything 的多模态 parser。
多模态（PDF/图像/表格）场景若有需要，应在独立入口中显式加载 RAGAnything，
且该入口必须保证 parser 已正确安装。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("EMBEDDINGS_BASE_URL") or "https://api.openai.com/v1"
API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "3072"))
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", BASE_URL)
LLM_API_KEY = os.environ.get("LLM_API_KEY", API_KEY)
WS = Path(os.environ.get("WORKSPACE", Path(__file__).resolve().parents[1]))
RAG_SEARCH_MODE = os.environ.get("RAG_SEARCH_MODE", "hybrid")

_lightrag_instances: dict[str, Any] = {}
_lightrag_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


async def _embed_func(texts: list[str]):
    import httpx
    import numpy as np
    url = f"{BASE_URL.rstrip('/')}/embeddings"
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            url,
            json={"model": EMBEDDING_MODEL, "input": texts},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        sorted_data = sorted(data, key=lambda x: x["index"])
        return np.array([d["embedding"] for d in sorted_data], dtype="float32")


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


async def get_lightrag(container: str) -> Any:
    """获取（必要时创建并初始化）container 对应的 LightRAG 实例。"""
    instance = _lightrag_instances.get(container)
    if instance is not None:
        return instance

    lock = await _get_lock(container)
    async with lock:
        instance = _lightrag_instances.get(container)
        if instance is not None:
            return instance

        from lightrag import LightRAG
        from lightrag.kg.shared_storage import initialize_pipeline_status
        from lightrag.utils import EmbeddingFunc

        working_dir = _container_working_dir(container)
        embedding_func = EmbeddingFunc(
            embedding_dim=EMBEDDING_DIM,
            max_token_size=8192,
            func=_embed_func,
        )

        instance = LightRAG(
            working_dir=str(working_dir),
            llm_model_func=_llm_func,
            embedding_func=embedding_func,
        )
        await instance.initialize_storages()
        await initialize_pipeline_status()

        _lightrag_instances[container] = instance
        logger.info("LightRAG instance initialized for container=%s at %s", container, working_dir)
        return instance


def clear_rag_cache() -> None:
    _lightrag_instances.clear()
    _lightrag_locks.clear()
