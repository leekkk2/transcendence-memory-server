"""架构自动检测模块 — 根据已安装包和环境变量判断 RAG 架构级别。"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field


@dataclass
class ModuleInfo:
    """单个 RAG 模块的状态信息。"""

    name: str
    enabled: bool = False
    ready: bool = False
    package_available: bool = False
    required_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)


@dataclass
class ArchitectureInfo:
    """整体架构检测结果。"""

    name: str
    modules: dict[str, ModuleInfo] = field(default_factory=dict)
    configured_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    optional_keys: list[str] = field(default_factory=list)


_cached: ArchitectureInfo | None = None


def detect_architecture(*, use_cache: bool = True) -> ArchitectureInfo:
    """检测当前 RAG 架构级别。

    根据已安装的 Python 包和环境变量自动判断：
    - lancedb-only：仅 LanceDB 向量检索
    - lancedb+lightrag：LanceDB + LightRAG 知识图谱
    - rag-everything：全部能力（含多模态 PDF/图片/表格解析）
    """
    global _cached
    if use_cache and _cached is not None:
        return _cached

    # 包可用性
    has_lancedb = importlib.util.find_spec('lancedb') is not None
    has_lightrag = importlib.util.find_spec('lightrag') is not None
    has_raganything = importlib.util.find_spec('raganything') is not None

    # 环境变量
    embedding_key = bool(os.environ.get('EMBEDDING_API_KEY'))
    llm_key = bool(os.environ.get('LLM_API_KEY'))
    vlm_key = bool(os.environ.get('VLM_API_KEY'))

    # 各模块状态
    lancedb_mod = ModuleInfo(
        name='lancedb',
        package_available=has_lancedb,
        enabled=has_lancedb,
        ready=has_lancedb and embedding_key,
        required_keys=['EMBEDDING_API_KEY'],
        missing_keys=[] if embedding_key else ['EMBEDDING_API_KEY'],
    )
    lightrag_mod = ModuleInfo(
        name='lightrag',
        package_available=has_lightrag,
        enabled=has_lightrag and llm_key and embedding_key,
        ready=has_lightrag and llm_key and embedding_key,
        required_keys=['LLM_API_KEY', 'EMBEDDING_API_KEY'],
        missing_keys=[k for k, v in [('LLM_API_KEY', llm_key), ('EMBEDDING_API_KEY', embedding_key)] if not v],
    )
    # 多模态依赖 LightRAG（raganything 基于 lightrag）
    multimodal_mod = ModuleInfo(
        name='multimodal',
        package_available=has_raganything,
        enabled=has_raganything and vlm_key and lightrag_mod.enabled,
        ready=has_raganything and vlm_key and lightrag_mod.enabled,
        required_keys=['VLM_API_KEY', 'LLM_API_KEY', 'EMBEDDING_API_KEY'],
        missing_keys=[k for k, v in [('VLM_API_KEY', vlm_key), ('LLM_API_KEY', llm_key), ('EMBEDDING_API_KEY', embedding_key)] if not v],
    )

    # 架构名称
    if multimodal_mod.enabled:
        arch_name = 'rag-everything'
    elif lightrag_mod.enabled:
        arch_name = 'lancedb+lightrag'
    else:
        arch_name = 'lancedb-only'

    # key 汇总
    all_keys = {
        'RAG_API_KEY': bool(os.environ.get('RAG_API_KEY')),
        'EMBEDDING_API_KEY': embedding_key,
        'LLM_API_KEY': llm_key,
        'VLM_API_KEY': vlm_key,
    }

    info = ArchitectureInfo(
        name=arch_name,
        modules={'lancedb': lancedb_mod, 'lightrag': lightrag_mod, 'multimodal': multimodal_mod},
        configured_keys=[k for k, v in all_keys.items() if v],
        missing_keys=[k for k, v in all_keys.items() if not v],
        optional_keys=['RAG_SEARCH_MODE', 'EMBEDDING_MODEL', 'EMBEDDING_DIM', 'LLM_MODEL', 'VLM_MODEL'],
    )
    _cached = info
    return info


def reset_cache() -> None:
    """清除缓存（用于测试）。"""
    global _cached
    _cached = None
