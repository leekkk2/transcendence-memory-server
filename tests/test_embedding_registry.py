"""embedding_registry 单元测试：resolve 匹配、build_embedding_func 行为、signature 协议。"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from lightrag.utils import EmbeddingFunc

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.embedding_registry import (
    EmbeddingRegistry,
    clear_registry,
    get_registry,
)
from scripts.profiles_loader import (
    EmbeddingProfile,
    ProfileSet,
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


def _make_ps_with_routes(monkeypatch, tmp_path) -> ProfileSet:
    """构造一份多 matcher 类型的 ProfileSet 给 resolve 测试用。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K1", "k1")
    monkeypatch.setenv("K2", "k2")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: p1
    model: m1
    dim: 3072
    base_url: https://a/v1
    api_key_env: K1
  - name: p2
    model: m2
    dim: 3072
    base_url: https://b/v1
    api_key_env: K2
routes:
  - match: {exact: host-z}
    embedding: p1
  - match: {glob: "test-*"}
    embedding: p2
  - match: {regex: "^v[0-9]+_.*$"}
    embedding: p1
  - match: {default: true}
    embedding: p2
""",
    )
    return load_profiles(str(yaml_path))


# ---- resolve 匹配 -------------------------------------------------------

def test_resolve_exact_match(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("host-z")
    assert route.embedding == "p1"


def test_resolve_glob_match(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    assert reg.resolve("test-alpha").embedding == "p2"
    assert reg.resolve("test-beta-1").embedding == "p2"


def test_resolve_regex_match(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    assert reg.resolve("v123_prod").embedding == "p1"


def test_resolve_falls_back_to_default(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    assert reg.resolve("random-container").embedding == "p2"


def test_resolve_order_first_match_wins(monkeypatch, tmp_path):
    """exact 在 glob 前面定义 → exact 命中即返回，不再去 match glob。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: a
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
  - name: b
    model: m
    dim: 8
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {exact: foo}
    embedding: a
  - match: {glob: "foo*"}
    embedding: b
  - match: {default: true}
    embedding: a
""",
    )
    ps = load_profiles(str(yaml_path))
    reg = EmbeddingRegistry(ps)
    assert reg.resolve("foo").embedding == "a"
    assert reg.resolve("foobar").embedding == "b"


# ---- get_profile --------------------------------------------------------

def test_get_profile_hit(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    assert reg.get_profile("p1").model == "m1"


def test_get_profile_miss_raises(monkeypatch, tmp_path):
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    with pytest.raises(KeyError, match=r"ghost"):
        reg.get_profile("ghost")


# ---- build_embedding_func -----------------------------------------------

def test_build_embedding_func_returns_lightrag_embeddingfunc(monkeypatch, tmp_path):
    """EmbeddingFunc 实例可用，dim 属性 = primary.dim。"""
    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("host-z")
    func, sig = reg.build_embedding_func(route)
    assert isinstance(func, EmbeddingFunc)
    assert func.embedding_dim == 3072
    assert func.max_token_size == 8192
    assert sig == "embed:p1"


def test_signature_includes_fallback_chain(monkeypatch, tmp_path):
    """signature 必须把 fallback 名按序拼上去，作为 cache key 一部分。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K1", "v1")
    monkeypatch.setenv("K2", "v2")
    monkeypatch.setenv("K3", "v3")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: gemini-3072
    model: gemini-embedding-001
    dim: 3072
    base_url: https://x/v1
    api_key_env: K1
  - name: openai-3072
    model: text-embedding-3-large
    dim: 3072
    base_url: https://x/v1
    api_key_env: K2
  - name: backup-3072
    model: backup
    dim: 3072
    base_url: https://x/v1
    api_key_env: K3
routes:
  - match: {default: true}
    embedding: gemini-3072
    embedding_fallbacks: [openai-3072, backup-3072]
""",
    )
    ps = load_profiles(str(yaml_path))
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("anything")
    _, sig = reg.build_embedding_func(route)
    assert sig == "embed:gemini-3072+openai-3072+backup-3072"


def test_embedding_func_invokes_http_with_correct_payload(monkeypatch, tmp_path):
    """Mock _http_embed → 验证 EmbeddingFunc 把 texts 透传，返回的 numpy 数组维度正确。"""
    import asyncio

    import scripts.embedding_registry as reg_mod

    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("host-z")  # p1, dim=3072

    captured: list[tuple[str, list[str]]] = []

    async def fake_http_embed(profile: EmbeddingProfile, texts: list[str]):
        captured.append((profile.name, list(texts)))
        return np.zeros((len(texts), profile.dim), dtype="float32")

    monkeypatch.setattr(reg_mod, "_http_embed", fake_http_embed)
    func, _ = reg.build_embedding_func(route)

    result = asyncio.run(func(["hello", "world"]))
    assert result.shape == (2, 3072)
    assert captured == [("p1", ["hello", "world"])]


def test_embedding_func_dim_validation_kicks_in(monkeypatch, tmp_path):
    """如果底层返回 wrong dim，EmbeddingFunc.__call__ 必须抛 ValueError。"""
    import asyncio

    import scripts.embedding_registry as reg_mod

    ps = _make_ps_with_routes(monkeypatch, tmp_path)
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("host-z")  # dim=3072

    async def bad_http_embed(profile, texts):
        return np.zeros((len(texts), 100), dtype="float32")  # wrong dim

    monkeypatch.setattr(reg_mod, "_http_embed", bad_http_embed)
    func, _ = reg.build_embedding_func(route)

    with pytest.raises(ValueError, match=r"dimension"):
        asyncio.run(func(["x"]))


def test_build_embedding_func_request_dim_passes_through(monkeypatch, tmp_path):
    """request_dim 设置时应通过 _http_embed body 透传给上游（这里只校验 profile 字段）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - name: matr
    model: text-embedding-3-large
    dim: 1024
    request_dim: 1024
    base_url: https://x/v1
    api_key_env: K
routes:
  - match: {default: true}
    embedding: matr
""",
    )
    ps = load_profiles(str(yaml_path))
    reg = EmbeddingRegistry(ps)
    route = reg.resolve("any")
    func, sig = reg.build_embedding_func(route)
    assert func.embedding_dim == 1024
    assert reg.get_profile("matr").request_dim == 1024


# ---- global singleton ---------------------------------------------------

def test_get_registry_singleton_and_clear(monkeypatch):
    """get_registry 返回单例；clear_registry 后能拿到新实例。"""
    _clear_legacy_env(monkeypatch)
    clear_registry()
    r1 = get_registry()
    r2 = get_registry()
    assert r1 is r2
    clear_registry()
    r3 = get_registry()
    assert r3 is not r1
    clear_registry()
