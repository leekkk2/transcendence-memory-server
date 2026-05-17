"""v0.7.0 back-compat：无 YAML + 仅 legacy env 时，loader 行为应与 rag_engine.py 改造前等价。

验证项：
1. 只设置 legacy EMBEDDING_* env、不放 YAML → 1 个 legacy profile + 1 个 default route
2. legacy profile 的 model/dim/base_url/api_key 与 env 完全一致
3. EmbeddingRegistry.resolve(任意 container) 一律返回 default route → legacy profile
4. build_embedding_func 的 EmbeddingFunc.embedding_dim 与 EMBEDDING_DIM env 一致
5. EMBEDDINGS_BASE_URL 备用变量也能被识别（rag_engine.py 旧逻辑）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lightrag.utils import EmbeddingFunc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.embedding_registry import EmbeddingRegistry, clear_registry
from scripts.profiles_loader import load_profiles


def _clear_all(monkeypatch):
    for k in (
        "EMBEDDING_MODEL",
        "EMBEDDING_DIM",
        "EMBEDDING_BASE_URL",
        "EMBEDDINGS_BASE_URL",
        "EMBEDDING_API_KEY",
        "TM_PROFILES_FILE",
        "WORKSPACE",
    ):
        monkeypatch.delenv(k, raising=False)
    clear_registry()


def test_legacy_env_yields_single_default_profile(monkeypatch):
    """旧 env 唯一 → 1 个 legacy profile + default route，行为等价旧 rag_engine.py。"""
    _clear_all(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setenv("EMBEDDING_DIM", "3072")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-test")

    ps = load_profiles()
    assert list(ps.embeddings.keys()) == ["legacy"]
    p = ps.embeddings["legacy"]
    assert p.model == "gemini-embedding-001"
    assert p.dim == 3072
    assert p.base_url == "https://api.openai.com/v1"
    assert p.api_key == "sk-test"
    assert ps.routes == []
    assert ps.default_route is not None
    assert ps.default_route.embedding == "legacy"
    assert ps.default_route.rerank_enabled is False
    assert ps.default_route.embedding_fallbacks == ()


def test_legacy_resolve_any_container_returns_default(monkeypatch):
    """resolve(任意 container) 都走 default → legacy profile。模拟旧版"全部走同一份 embedding"行为。"""
    _clear_all(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "gemini-embedding-001")
    monkeypatch.setenv("EMBEDDING_DIM", "3072")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-x")

    reg = EmbeddingRegistry(load_profiles())
    for container in ("default", "my-container", "test-foo", "v1_prod", "anything-else"):
        route = reg.resolve(container)
        assert route.embedding == "legacy"
        assert route.embedding_fallbacks == ()


def test_legacy_build_embedding_func_dim_matches_env(monkeypatch):
    """EmbeddingFunc.embedding_dim 必须与 EMBEDDING_DIM env 完全一致。"""
    _clear_all(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "custom-model")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-y")

    reg = EmbeddingRegistry(load_profiles())
    route = reg.resolve("legacy-container")
    func, sig = reg.build_embedding_func(route)
    assert isinstance(func, EmbeddingFunc)
    assert func.embedding_dim == 1024
    assert sig == "embed:legacy"


def test_legacy_alt_base_url_var_recognized(monkeypatch):
    """EMBEDDINGS_BASE_URL（带 s 的备用变量名）与 rag_engine.py 旧逻辑一致地被识别。"""
    _clear_all(monkeypatch)
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "https://alt.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-z")

    ps = load_profiles()
    assert ps.embeddings["legacy"].base_url == "https://alt.test/v1"


def test_legacy_uses_hardcoded_defaults_when_env_empty(monkeypatch):
    """完全空 env：使用与旧 rag_engine.py 一致的硬编码 default（gemini-embedding-001 / 3072）。"""
    _clear_all(monkeypatch)
    ps = load_profiles()
    p = ps.embeddings["legacy"]
    assert p.model == "gemini-embedding-001"
    assert p.dim == 3072
    assert p.base_url == "https://api.openai.com/v1"


def test_legacy_no_api_key_allowed(monkeypatch):
    """legacy 路径下 EMBEDDING_API_KEY 缺失/空也能 load_profiles 成功，
    与 v0.6 行为一致（实际请求会 401，但启动不应被阻挡）。"""
    _clear_all(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    # 故意不设 EMBEDDING_API_KEY
    ps = load_profiles()
    assert ps.embeddings["legacy"].api_key == ""
