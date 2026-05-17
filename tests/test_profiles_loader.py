"""profiles_loader 单元测试：YAML 解析、legacy 合成、校验报错。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.profiles_loader import (
    EmbeddingProfile,
    ProfileSet,
    RerankerProfile,
    Route,
    load_profiles,
)


# ---- helpers ------------------------------------------------------------

def _write_yaml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "profiles.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _clear_legacy_env(monkeypatch):
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


# ---- YAML 解析正常路径 --------------------------------------------------

def test_load_yaml_full_schema(tmp_path, monkeypatch):
    """完整 schema：2 个 embedding + 1 个 reranker + 2 条 route（含 default + exact）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")
    monkeypatch.setenv("RERANKER_API_KEY", "selfhosted-gateway-key")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: gemini-3072
    provider: openai_compatible
    model: gemini-embedding-001
    dim: 3072
    base_url: https://newapi.example/v1
    api_key_env: GEMINI_API_KEY
    max_token_size: 8000
    request_dim: null
    timeout_s: 45
    max_retries: 5
  - name: openai-3072
    provider: openai_compatible
    model: text-embedding-3-large
    dim: 3072
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
    request_dim: 3072
rerankers:
  - name: selfhosted-bge
    provider: cohere_compatible
    model: text-reranker
    base_url: https://newapi.example/v1
    api_key_env: RERANKER_API_KEY
    timeout_s: 25
    min_score: 0.1
routes:
  - match: {exact: default}
    embedding: gemini-3072
    embedding_fallbacks: [openai-3072]
    reranker: selfhosted-bge
    rerank: {enabled: true, chunk_top_k: 40, top_k: 10}
  - match: {default: true}
    embedding: gemini-3072
""",
    )
    ps = load_profiles(str(yaml_path))

    assert isinstance(ps, ProfileSet)
    assert set(ps.embeddings) == {"gemini-3072", "openai-3072"}
    assert set(ps.rerankers) == {"selfhosted-bge"}
    gem = ps.embeddings["gemini-3072"]
    assert isinstance(gem, EmbeddingProfile)
    assert gem.dim == 3072
    assert gem.max_token_size == 8000
    assert gem.timeout_s == 45
    assert gem.max_retries == 5
    assert gem.request_dim is None
    assert gem.api_key == "gem-key"

    oa = ps.embeddings["openai-3072"]
    assert oa.request_dim == 3072

    rr = ps.rerankers["selfhosted-bge"]
    assert isinstance(rr, RerankerProfile)
    assert rr.min_score == 0.1
    assert rr.timeout_s == 25

    assert len(ps.routes) == 1
    matcher, route = ps.routes[0]
    assert matcher == {"exact": "default"}
    assert route.embedding == "gemini-3072"
    assert route.embedding_fallbacks == ("openai-3072",)
    assert route.reranker == "selfhosted-bge"
    assert route.rerank_enabled is True
    assert route.chunk_top_k == 40
    assert route.top_k == 10

    assert ps.default_route is not None
    assert ps.default_route.embedding == "gemini-3072"
    assert ps.default_route.rerank_enabled is False


def test_repr_redacts_api_key(monkeypatch, tmp_path):
    """EmbeddingProfile/RerankerProfile.__repr__ 必须脱敏，防止日志泄漏。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("MY_KEY", "secret-do-not-leak")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m1
    dim: 8
    base_url: https://x/v1
    api_key_env: MY_KEY
rerankers:
  - name: r1
    model: rr
    base_url: https://x/v1
    api_key_env: MY_KEY
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert "secret-do-not-leak" not in repr(ps.embeddings["p1"])
    assert "***" in repr(ps.embeddings["p1"])
    assert "secret-do-not-leak" not in repr(ps.rerankers["r1"])
    assert "***" in repr(ps.rerankers["r1"])


# ---- legacy env 合成 ----------------------------------------------------

def test_load_legacy_env_when_no_yaml(monkeypatch, tmp_path):
    """没 YAML 时合成 legacy profile + default route。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "my-model")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://legacy.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "legacy-key")

    ps = load_profiles()
    assert list(ps.embeddings) == ["legacy"]
    p = ps.embeddings["legacy"]
    assert p.model == "my-model"
    assert p.dim == 1024
    assert p.base_url == "https://legacy.test/v1"
    assert p.api_key == "legacy-key"
    assert ps.default_route is not None
    assert ps.default_route.embedding == "legacy"
    assert ps.routes == []


def test_legacy_env_falls_back_to_defaults(monkeypatch):
    """全空 env 也能起来，使用 hardcoded defaults。"""
    _clear_legacy_env(monkeypatch)
    ps = load_profiles()
    p = ps.embeddings["legacy"]
    assert p.model == "gemini-embedding-001"
    assert p.dim == 3072


def test_yaml_path_from_env(monkeypatch, tmp_path):
    """env TM_PROFILES_FILE 优先生效。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("MY_KEY", "k")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 4
    base_url: https://x/v1
    api_key_env: MY_KEY
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    monkeypatch.setenv("TM_PROFILES_FILE", str(yaml_path))
    ps = load_profiles()
    assert "p1" in ps.embeddings


# ---- 校验错误 -----------------------------------------------------------

def test_fallback_dim_mismatch_raises(monkeypatch, tmp_path):
    """fallback 与 primary dim 不一致必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K1", "v1")
    monkeypatch.setenv("K2", "v2")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m1
    dim: 3072
    base_url: https://x/v1
    api_key_env: K1
  - name: p2
    model: m2
    dim: 1024
    base_url: https://x/v1
    api_key_env: K2
routes:
  - match: {default: true}
    embedding: p1
    embedding_fallbacks: [p2]
""",
    )
    with pytest.raises(ValueError, match=r"dim.*不一致"):
        load_profiles(str(yaml_path))


def test_missing_api_key_env_raises(monkeypatch, tmp_path):
    """引用的 api_key_env 在环境里找不到必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.delenv("MISSING_KEY", raising=False)
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: MISSING_KEY
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    with pytest.raises(ValueError, match=r"MISSING_KEY"):
        load_profiles(str(yaml_path))


def test_no_default_route_raises(monkeypatch, tmp_path):
    """必须恰好 1 个 default route。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {exact: default}
    embedding: p1
""",
    )
    with pytest.raises(ValueError, match=r"default"):
        load_profiles(str(yaml_path))


def test_multiple_default_routes_raises(monkeypatch, tmp_path):
    """多个 default 也不行。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
  - match: {default: true}
    embedding: p1
""",
    )
    with pytest.raises(ValueError, match=r"multiple default"):
        load_profiles(str(yaml_path))


def test_duplicate_profile_name_raises(monkeypatch, tmp_path):
    """同名 profile 重复定义必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
  - name: p1
    model: m2
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    with pytest.raises(ValueError, match=r"duplicate"):
        load_profiles(str(yaml_path))


def test_route_references_unknown_embedding(monkeypatch, tmp_path):
    """route 引用不存在的 embedding profile 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: ghost
""",
    )
    with pytest.raises(ValueError, match=r"ghost"):
        load_profiles(str(yaml_path))


def test_route_references_unknown_reranker(monkeypatch, tmp_path):
    """route 引用不存在的 reranker 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
    reranker: nope
""",
    )
    with pytest.raises(ValueError, match=r"nope"):
        load_profiles(str(yaml_path))


def test_unsupported_version_raises(monkeypatch, tmp_path):
    """不支持的 schema version 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 99
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    with pytest.raises(ValueError, match=r"version"):
        load_profiles(str(yaml_path))


# ---- v0.11.0：union_search_default 顶层字段 -----------------------------

def test_union_search_default_absent_defaults_to_false(monkeypatch, tmp_path):
    """YAML 不含 union_search_default 字段 → 默认 false（向后兼容）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert ps.union_search_default is False


def test_union_search_default_true_parsed(monkeypatch, tmp_path):
    """YAML 显式 union_search_default: true → 解析为 True。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
union_search_default: true
embeddings:
  - name: p1
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert ps.union_search_default is True


def test_union_search_default_legacy_env_is_false(monkeypatch):
    """无 YAML（legacy env-only）→ union_search_default 必须 false（不破坏旧部署）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    ps = load_profiles()
    assert ps.union_search_default is False
