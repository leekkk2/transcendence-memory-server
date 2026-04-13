#!/usr/bin/env python3
"""RAGAnything engine — 多模态文档解析与入库。

直接复用 rag_engine 创建的 LightRAG 实例作为 `lightrag=` 参数注入 RAGAnything，
保证同一 container 的纯文本入库（/documents/text）与多模态入库（/documents/file）
写入同一个知识图谱 working_dir。

所有业务入口：
    rag = await get_raganything(container)
    await rag.process_document_complete(file_path=..., output_dir=..., parse_method="auto")
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

try:
    from rag_engine import (
        _embed_func,
        _llm_func,
        _container_working_dir,
        get_lightrag,
        LLM_BASE_URL as _RAG_LLM_BASE_URL,
        LLM_API_KEY as _RAG_LLM_API_KEY,
        LLM_MODEL as _RAG_LLM_MODEL,
    )
except ModuleNotFoundError:  # pragma: no cover
    from scripts.rag_engine import (  # type: ignore
        _embed_func,
        _llm_func,
        _container_working_dir,
        get_lightrag,
        LLM_BASE_URL as _RAG_LLM_BASE_URL,
        LLM_API_KEY as _RAG_LLM_API_KEY,
        LLM_MODEL as _RAG_LLM_MODEL,
    )

logger = logging.getLogger(__name__)

VLM_MODEL = os.environ.get("VLM_MODEL") or _RAG_LLM_MODEL
VLM_BASE_URL = os.environ.get("VLM_BASE_URL") or _RAG_LLM_BASE_URL
VLM_API_KEY = os.environ.get("VLM_API_KEY") or _RAG_LLM_API_KEY

_SUPPORTED_PARSERS = {"mineru", "docling"}

_instances: dict[str, Any] = {}
_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


async def _vision_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, Any]] | None = None,
    image_data: str | list[str] | None = None,
    messages: list[dict[str, Any]] | None = None,
    **_: Any,
) -> str:
    """OpenAI 兼容的 vision chat/completions 调用。

    兼容 RAGAnything 既有两种调用签名：
      1) 直接传 `messages=[...]`（含混排 text/image_url content）
      2) 传 `prompt` + `image_data`（base64 编码的单张或多张图片）
    """
    import httpx

    url = f"{VLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {VLM_API_KEY}"}

    if messages is None:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        images: list[str] = []
        if isinstance(image_data, str):
            images = [image_data]
        elif isinstance(image_data, list):
            images = list(image_data)
        for img in images:
            url_field = img if img.startswith("http") or img.startswith("data:") else f"data:image/jpeg;base64,{img}"
            content.append({"type": "image_url", "image_url": {"url": url_field}})
        msgs: list[dict[str, Any]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if history_messages:
            msgs.extend(history_messages)
        msgs.append({"role": "user", "content": content})
    else:
        msgs = messages

    payload = {"model": VLM_MODEL, "messages": msgs}
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        out = data["choices"][0]["message"].get("content")
        if not out:
            raise ValueError(f"VLM returned empty content: {data}")
        return out


async def _get_lock(container: str) -> asyncio.Lock:
    async with _global_lock:
        if container not in _locks:
            _locks[container] = asyncio.Lock()
        return _locks[container]


async def get_raganything(container: str) -> Any:
    """获取 / 创建 container 对应的 RAGAnything 实例，复用已有 LightRAG。"""
    instance = _instances.get(container)
    if instance is not None:
        return instance

    lock = await _get_lock(container)
    async with lock:
        instance = _instances.get(container)
        if instance is not None:
            return instance

        from raganything import RAGAnything, RAGAnythingConfig
        from lightrag.utils import EmbeddingFunc

        lightrag = await get_lightrag(container)
        working_dir = _container_working_dir(container)

        embedding_func = EmbeddingFunc(
            embedding_dim=int(os.environ.get("EMBEDDING_DIM", "3072")),
            max_token_size=8192,
            func=_embed_func,
        )

        parser_name = os.environ.get("RAG_PARSER", "mineru")
        if parser_name not in _SUPPORTED_PARSERS:
            raise ValueError(
                f"unsupported RAG_PARSER={parser_name!r}; expected one of {sorted(_SUPPORTED_PARSERS)}"
            )

        config = RAGAnythingConfig(
            working_dir=str(working_dir),
            parser=parser_name,
            parse_method=os.environ.get("RAG_PARSE_METHOD", "auto"),
            enable_image_processing=True,
            enable_table_processing=True,
            enable_equation_processing=True,
        )

        instance = RAGAnything(
            lightrag=lightrag,
            llm_model_func=_llm_func,
            vision_model_func=_vision_model_func,
            embedding_func=embedding_func,
            config=config,
        )

        # 触发 parser 校验与处理器初始化；失败时显式抛错
        result = await instance._ensure_lightrag_initialized()
        if isinstance(result, dict) and result.get("success") is False:
            raise RuntimeError(
                f"RAGAnything init failed for container={container}: {result.get('error')}"
            )

        _instances[container] = instance
        logger.info(
            "RAGAnything instance ready for container=%s at %s (parser=%s)",
            container, working_dir, config.parser,
        )
        return instance


def clear_cache() -> None:
    _instances.clear()
    _locks.clear()
