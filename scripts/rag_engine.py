#!/usr/bin/env python3
"""RAG-Anything engine module — manages per-container RAGAnything instances.

Provides full multimodal RAG: PDF/image/table parsing + knowledge graph + hybrid retrieval.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# --- 环境变量配置 ---
BASE_URL = os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("EMBEDDINGS_BASE_URL") or "https://newapi.zweiteng.tk/v1"
API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "3072"))
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", BASE_URL)
LLM_API_KEY = os.environ.get("LLM_API_KEY", API_KEY)
VLM_MODEL = os.environ.get("VLM_MODEL", "qwen3-vl-plus")
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", BASE_URL)
VLM_API_KEY = os.environ.get("VLM_API_KEY", API_KEY)
WS = Path(os.environ.get("WORKSPACE", "/home/ubuntu/.openclaw/workspace"))
RAG_SEARCH_MODE = os.environ.get("RAG_SEARCH_MODE", "hybrid")

# --- Container -> RAGAnything 实例缓存 ---
_rag_instances: dict[str, Any] = {}
_rag_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


async def _embed_func(texts: list[str]):
    """异步 embedding，调用 OpenAI 兼容 API。返回 numpy array。"""
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


async def _llm_func(prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
    """异步 LLM，调用 OpenAI 兼容 chat/completions API。"""
    import httpx
    url = f"{LLM_BASE_URL.rstrip('/')}/chat/completions"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    body: dict[str, Any] = {"model": LLM_MODEL, "messages": messages}
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            url, json=body,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def _vision_model_func(prompt: str, images: list[str] | None = None, **kwargs: Any) -> str:
    """异步 VLM，调用 OpenAI 兼容多模态 chat/completions API。"""
    import httpx
    url = f"{VLM_BASE_URL.rstrip('/')}/chat/completions"
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images or []:
        content.append({"type": "image_url", "image_url": {"url": img}})
    body: dict[str, Any] = {
        "model": VLM_MODEL,
        "messages": [{"role": "user", "content": content}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url, json=body,
            headers={"Authorization": f"Bearer {VLM_API_KEY}"},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _container_working_dir(container: str) -> Path:
    path = WS / "tasks" / "rag" / "containers" / container / "raganything"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _get_lock(container: str) -> asyncio.Lock:
    async with _global_lock:
        if container not in _rag_locks:
            _rag_locks[container] = asyncio.Lock()
        return _rag_locks[container]


async def get_rag(container: str) -> Any:
    """获取或创建 container 对应的 RAGAnything 实例。"""
    if container in _rag_instances:
        return _rag_instances[container]

    lock = await _get_lock(container)
    async with lock:
        if container in _rag_instances:
            return _rag_instances[container]

        from raganything import RAGAnything, RAGAnythingConfig
        from lightrag.utils import EmbeddingFunc

        working_dir = _container_working_dir(container)

        embedding_func = EmbeddingFunc(
            embedding_dim=EMBEDDING_DIM,
            max_token_size=8192,
            func=_embed_func,
        )

        config = RAGAnythingConfig(
            working_dir=str(working_dir),
            enable_image_processing=True,
            enable_table_processing=True,
            enable_equation_processing=True,
        )

        rag = RAGAnything(
            llm_model_func=_llm_func,
            vision_model_func=_vision_model_func,
            embedding_func=embedding_func,
            config=config,
        )

        _rag_instances[container] = rag
        logger.info("RAGAnything instance created for container=%s at %s", container, working_dir)
        return rag


async def ensure_rag_initialized(container: str) -> Any:
    """确保 RAGAnything 实例已初始化（含 LightRAG 存储初始化）。"""
    rag = await get_rag(container)
    await rag._ensure_lightrag_initialized()
    # LightRAG 1.4.x 需要显式初始化存储
    if hasattr(rag.lightrag, 'initialize_storages'):
        await rag.lightrag.initialize_storages()
    return rag


def clear_rag_cache() -> None:
    """清除缓存（用于测试）。"""
    _rag_instances.clear()
    _rag_locks.clear()
