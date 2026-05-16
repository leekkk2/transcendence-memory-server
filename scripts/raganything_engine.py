#!/usr/bin/env python3
"""RAGAnything engine — 多模态文档解析与入库。

直接复用 rag_engine 创建的 LightRAG 实例作为 `lightrag=` 参数注入 RAGAnything，
保证同一 container 的纯文本入库（/documents/text）与多模态入库（/documents/file）
写入同一个知识图谱 working_dir。

所有业务入口：
    rag = await get_raganything(container)
    await rag.process_document_complete(file_path=..., output_dir=..., parse_method="auto")

v0.7.0 multi-embedding 升级（2026-05-16）：
  - EmbeddingFunc 不再由本模块构造，改为通过 embedding_registry 按 container
    解析 route -> profile 后由 registry.build_embedding_func 提供。
  - cache key 升级为 (container, route_sig)，与 rag_engine 保持一致，避免
    route 切换后 RAGAnything 与底层 LightRAG instance 维度不一致。
  - VLM 逻辑独立保留（本 phase 不动 VLM）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

try:
    from rag_engine import (
        _llm_func,
        _container_working_dir,
        call_openai_chat,
        get_lightrag,
        LLM_BASE_URL as _RAG_LLM_BASE_URL,
        LLM_API_KEY as _RAG_LLM_API_KEY,
        LLM_MODEL as _RAG_LLM_MODEL,
    )
except ModuleNotFoundError:  # pragma: no cover
    from scripts.rag_engine import (  # type: ignore
        _llm_func,
        _container_working_dir,
        call_openai_chat,
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

# cache key = (container, route_sig)，与 rag_engine._lightrag_instances 对齐
_instances: dict[tuple[str, str], Any] = {}
_locks: dict[str, asyncio.Lock] = {}
_global_lock = asyncio.Lock()


def _image_url_field(img: str) -> str:
    """把 raganything 传来的图像字段归一成 chat/completions 的 image_url.url。"""
    if img.startswith("http://") or img.startswith("https://") or img.startswith("data:"):
        return img
    # 本地文件路径 → 读取并 base64 编码
    if os.path.isabs(img) and os.path.exists(img):
        import base64, mimetypes
        mime = mimetypes.guess_type(img)[0] or "image/png"
        with open(img, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return f"data:{mime};base64,{b64}"
    # 默认按 base64 字符串处理
    return f"data:image/png;base64,{img}"


async def _vision_model_func(
    prompt: str = "",
    system_prompt: str | None = None,
    history_messages: list[dict[str, Any]] | None = None,
    image_data: str | list[str] | None = None,
    messages: list[dict[str, Any]] | None = None,
    **_: Any,
) -> str:
    """OpenAI 兼容的 vision chat/completions 调用，带指数退避重试。

    兼容 RAGAnything 的两种调用签名：
      1) 直接传 `messages=[...]`（含混排 text/image_url content）
      2) 传 `prompt` + `image_data`（URL / data URI / 绝对路径 / base64 字符串）
    """
    if messages is None:
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        images: list[str] = []
        if isinstance(image_data, str):
            images = [image_data]
        elif isinstance(image_data, list):
            images = list(image_data)
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": _image_url_field(img)}})
        msgs: list[dict[str, Any]] = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        if history_messages:
            msgs.extend(history_messages)
        msgs.append({"role": "user", "content": content})
    else:
        msgs = messages

    return await call_openai_chat(
        base_url=VLM_BASE_URL,
        api_key=VLM_API_KEY,
        model=VLM_MODEL,
        messages=msgs,
        label="VLM",
    )


async def _get_lock(container: str) -> asyncio.Lock:
    async with _global_lock:
        if container not in _locks:
            _locks[container] = asyncio.Lock()
        return _locks[container]


def _resolve_route_and_embedding(container: str) -> tuple[Any, str, Any]:
    """通过 registry 解析 container -> route -> EmbeddingFunc。

    与 rag_engine._resolve_route_and_embedding 同结构；故意不互相依赖
    （避免循环 import），代价是少量重复。
    """
    try:
        from .embedding_registry import get_registry
    except ImportError:  # pragma: no cover
        from embedding_registry import get_registry  # type: ignore

    registry = get_registry()
    route = registry.resolve(container)
    embedding_func, route_sig = registry.build_embedding_func(route)
    return route, route_sig, embedding_func


async def get_raganything(container: str) -> Any:
    """获取 / 创建 container 对应的 RAGAnything 实例，复用已有 LightRAG。"""
    # 先解析 route，决定 cache key；同时拿到 EmbeddingFunc 注入给 RAGAnything。
    route, route_sig, embedding_func = _resolve_route_and_embedding(container)
    cache_key = (container, route_sig)

    instance = _instances.get(cache_key)
    if instance is not None:
        return instance

    lock = await _get_lock(container)
    async with lock:
        instance = _instances.get(cache_key)
        if instance is not None:
            return instance

        from raganything import RAGAnything, RAGAnythingConfig

        # get_lightrag 内部按相同 cache_key 复用 LightRAG instance，保证 RAGAnything
        # 与底层 LightRAG 使用同一个 embedding_func（同一 dim、同一 profile）。
        lightrag = await get_lightrag(container)
        working_dir = _container_working_dir(container)

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

        _instances[cache_key] = instance
        logger.info(
            "RAGAnything instance ready for container=%s route=%s at %s (parser=%s)",
            container, route_sig, working_dir, config.parser,
        )
        return instance


def clear_cache() -> None:
    _instances.clear()
    _locks.clear()
