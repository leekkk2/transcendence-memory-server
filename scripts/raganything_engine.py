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
import sys
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

# 通用 fallback 核心 —— VLM 视觉调用链路用 run_with_fallback 跨 profile 切换。
# 双重 import + 模块身份归一，与 embedding_registry 同款。
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

logger = logging.getLogger(__name__)

VLM_MODEL = os.environ.get("VLM_MODEL") or _RAG_LLM_MODEL
VLM_BASE_URL = os.environ.get("VLM_BASE_URL") or _RAG_LLM_BASE_URL
VLM_API_KEY = os.environ.get("VLM_API_KEY") or _RAG_LLM_API_KEY

_SUPPORTED_PARSERS = {"mineru", "docling"}

# cache key = (container, emb_sig, rrk_sig)，与 rag_engine._lightrag_instances 对齐。
# Phase 2：rrk_sig 进 cache key 让 reranker 切换强制重建 RAGAnything instance。
_instances: dict[tuple[str, str, str], Any] = {}
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


def _build_vision_messages(
    prompt: str,
    system_prompt: str | None,
    history_messages: list[dict[str, Any]] | None,
    image_data: str | list[str] | None,
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """把 RAGAnything 的两种 vision 调用签名归一为 chat/completions messages。

    1) 直接传 `messages=[...]`（含混排 text/image_url content）→ 原样返回。
    2) 传 `prompt` + `image_data`（URL / data URI / 绝对路径 / base64 字符串）
       → 拼成单条 user 消息（text + image_url content 混排）。
    """
    if messages is not None:
        return messages
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
    return msgs


def make_vision_model_func(chain: list) -> Any:
    """构造注入 RAGAnything 的 ``vision_model_func`` —— 按 chain fallback 的 VLM 调用。

    Args:
        chain: ``[primary, *fallbacks]`` 的 VLMProfile 列表。主 VLM 不可用
            （429/5xx/超时/typed quota 等）时由 run_with_fallback 切下一条；
            单元素链（无 fallback 配置 / legacy env）行为等价单 profile。

    Returns:
        async vision callable，兼容 RAGAnything 的两种调用签名（messages= 或
        prompt+image_data）。``call_openai_chat`` 保持为单 profile 执行器
        （per-profile 指数退避重试不变）。
    """
    async def _vision_model_func(
        prompt: str = "",
        system_prompt: str | None = None,
        history_messages: list[dict[str, Any]] | None = None,
        image_data: str | list[str] | None = None,
        messages: list[dict[str, Any]] | None = None,
        **_: Any,
    ) -> str:
        msgs = _build_vision_messages(
            prompt, system_prompt, history_messages, image_data, messages,
        )

        async def _executor(profile: Any) -> str:
            return await call_openai_chat(
                base_url=profile.base_url,
                api_key=profile.api_key,
                model=profile.model,
                messages=msgs,
                timeout=profile.timeout_s,
                label="VLM",
            )

        return await model_fallback.run_with_fallback(
            model_fallback.CATEGORY_VLM, chain, _executor,
        )

    return _vision_model_func


def _build_vlm_chain(route: Any) -> list:
    """按 route 解析 VLM fallback 链 ``[primary, *fallbacks]``。

    route.vlm 为 None（v2 yaml 未配 vlm）时合成单元素 legacy 链 —— 用 module
    级 env 常量构造，行为等价改造前 env-driven 的 `_vision_model_func`。
    """
    try:
        from profiles_loader import VLMProfile  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - package import path
        from scripts.profiles_loader import VLMProfile  # type: ignore[import-not-found]

    if route.vlm is None:
        return [VLMProfile(
            name="legacy-vlm",
            model=VLM_MODEL,
            base_url=VLM_BASE_URL,
            api_key=VLM_API_KEY,
        )]
    try:
        from embedding_registry import get_registry  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - package import path
        from scripts.embedding_registry import get_registry  # type: ignore[import-not-found]
    vlms = get_registry().profiles.vlms
    return [vlms[route.vlm], *(vlms[fb] for fb in route.vlm_fallbacks)]


async def _get_lock(container: str) -> asyncio.Lock:
    async with _global_lock:
        if container not in _locks:
            _locks[container] = asyncio.Lock()
        return _locks[container]


def _resolve_route_emb_rrk(
    container: str,
) -> tuple[Any, Any, str, str]:
    """通过 registry 解析 container -> route -> (embedding_func, rrk_sig)。

    Phase 2：仅取 emb_sig + rrk_sig 用于 cache key 计算；RAGAnything 内部
    嵌的 LightRAG 由 rag_engine.get_lightrag 单独注入 rerank_func，本模块
    不重复传 rerank_func（避免与底层 LightRAG instance 行为冲突）。

    与 rag_engine._resolve_route_emb_rrk 同结构；故意不互相依赖（避免循环
    import），代价是少量重复，换取模块独立可加载。
    """
    try:
        from .embedding_registry import get_registry
    except ImportError:  # pragma: no cover
        from embedding_registry import get_registry  # type: ignore

    registry = get_registry()
    route = registry.resolve(container)
    embedding_func, emb_sig = registry.build_embedding_func(route)
    # rrk_sig 含 reranker 主 + fallback 链名，与 reranker_registry.build_rerank_func
    # 产出的 sig 格式一致，保证 RAGAnything 与底层 LightRAG 的 cache key 对齐。
    if route.reranker:
        rrk_names = [route.reranker, *route.reranker_fallbacks]
        rrk_sig = "rerank:" + "+".join(rrk_names)
    else:
        rrk_sig = ""
    return route, embedding_func, emb_sig, rrk_sig


async def get_raganything(container: str) -> Any:
    """获取 / 创建 container 对应的 RAGAnything 实例，复用已有 LightRAG。"""
    # 先解析 route，决定 cache key；同时拿到 EmbeddingFunc 注入给 RAGAnything。
    # rerank 由底层 LightRAG instance 持有（get_lightrag 已注入），此处只用
    # rrk_sig 算 cache key，避免 RAGAnything 重复构造 rerank_func。
    route, embedding_func, emb_sig, rrk_sig = _resolve_route_emb_rrk(container)
    cache_key = (container, emb_sig, rrk_sig)

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

        # VLM 按 route 展开 fallback 链注入 —— 无 VLM profile 配置时 chain 退化
        # 为 legacy env 合成的单元素，行为等价改造前。
        vlm_chain = _build_vlm_chain(route)

        instance = RAGAnything(
            lightrag=lightrag,
            llm_model_func=_llm_func,
            vision_model_func=make_vision_model_func(vlm_chain),
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
            "RAGAnything instance ready for container=%s emb=%s rrk=%s at %s (parser=%s)",
            container, emb_sig, rrk_sig or "<none>", working_dir, config.parser,
        )
        return instance


def clear_cache() -> None:
    _instances.clear()
    _locks.clear()
