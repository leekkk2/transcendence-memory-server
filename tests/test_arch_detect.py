"""架构检测模块测试。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_arch_detect(monkeypatch, env: dict[str, str]):
    """重新加载 arch_detect 模块并返回。"""
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for mod_name in list(sys.modules):
        if 'arch_detect' in mod_name:
            sys.modules.pop(mod_name, None)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    mod = importlib.import_module('scripts.arch_detect')
    mod.reset_cache()
    return mod


def test_lancedb_only(monkeypatch):
    """无 LLM/VLM key 时应为 lancedb-only。"""
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('VLM_API_KEY', raising=False)
    mod = _load_arch_detect(monkeypatch, {'EMBEDDING_API_KEY': 'test-key'})
    arch = mod.detect_architecture(use_cache=False)
    assert arch.name == 'lancedb-only'
    assert arch.modules['lancedb'].enabled is True
    assert arch.modules['lightrag'].enabled is False
    assert arch.modules['multimodal'].enabled is False


def test_lancedb_plus_lightrag(monkeypatch):
    """有 LLM_API_KEY + EMBEDDING_API_KEY ��应为 lancedb+lightrag。"""
    monkeypatch.delenv('VLM_API_KEY', raising=False)
    mod = _load_arch_detect(monkeypatch, {
        'EMBEDDING_API_KEY': 'test-key',
        'LLM_API_KEY': 'test-llm-key',
    })
    arch = mod.detect_architecture(use_cache=False)
    # lightrag 包是否安装决定是否升级
    if arch.modules['lightrag'].package_available:
        assert arch.name == 'lancedb+lightrag'
        assert arch.modules['lightrag'].enabled is True
    else:
        assert arch.name == 'lancedb-only'
    assert arch.modules['multimodal'].enabled is False


def test_rag_everything(monkeypatch):
    """全部 key 配齐时应为 rag-everything（取决于包安装）。"""
    mod = _load_arch_detect(monkeypatch, {
        'EMBEDDING_API_KEY': 'test-key',
        'LLM_API_KEY': 'test-llm-key',
        'VLM_API_KEY': 'test-vlm-key',
    })
    arch = mod.detect_architecture(use_cache=False)
    if arch.modules['multimodal'].package_available:
        assert arch.name == 'rag-everything'
    elif arch.modules['lightrag'].package_available:
        assert arch.name == 'lancedb+lightrag'
    else:
        assert arch.name == 'lancedb-only'


def test_missing_keys_reported(monkeypatch):
    """缺失的 key 应在 missing_keys 中报告。"""
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('VLM_API_KEY', raising=False)
    monkeypatch.delenv('RAG_API_KEY', raising=False)
    mod = _load_arch_detect(monkeypatch, {'EMBEDDING_API_KEY': 'test-key'})
    arch = mod.detect_architecture(use_cache=False)
    assert 'LLM_API_KEY' in arch.missing_keys
    assert 'VLM_API_KEY' in arch.missing_keys
    assert 'RAG_API_KEY' in arch.missing_keys
    assert 'EMBEDDING_API_KEY' in arch.configured_keys


def test_cache_works(monkeypatch):
    """缓存应返回相同结果。"""
    mod = _load_arch_detect(monkeypatch, {'EMBEDDING_API_KEY': 'test-key'})
    a = mod.detect_architecture(use_cache=False)
    b = mod.detect_architecture(use_cache=True)
    assert a.name == b.name


def test_reset_cache(monkeypatch):
    """reset_cache 后应清除缓存。"""
    mod = _load_arch_detect(monkeypatch, {'EMBEDDING_API_KEY': 'test-key'})
    mod.detect_architecture(use_cache=False)
    mod.reset_cache()
    assert mod._cached is None
