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
    """全空 env 也能起来，使用 hardcoded defaults.

    2026-05-29: defaults flipped to text-embedding-3-small / 1024 after the
    EMBEDDING_MODEL drift incident (see workspace
    docs/decisions/2026-05-29-embedding-model-drift-incident.md).
    """
    _clear_legacy_env(monkeypatch)
    ps = load_profiles()
    p = ps.embeddings["legacy"]
    assert p.model == "text-embedding-3-small"
    assert p.dim == 1024


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


# ---- v2 schema：llms / vlms 节点 + route 的 LLM/VLM 字段 -----------------

def test_load_yaml_v2_with_llms_vlms(tmp_path, monkeypatch):
    """version 2：解析 llms / vlms 节点与 route 的 llm/llm_fallbacks/vlm 字段。"""
    _clear_legacy_env(monkeypatch)
    for k in ("EK", "LK", "VK"):
        monkeypatch.setenv(k, f"{k}-secret")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 2
embeddings:
  - {name: e1, model: em, dim: 8, base_url: https://x/v1, api_key_env: EK}
llms:
  - {name: l1, model: chat-a, base_url: https://x/v1, api_key_env: LK}
  - name: l2
    model: chat-b
    provider: gemini_native
    base_url: https://y/v1
    api_key_env: LK
    timeout_s: 90
    max_retries: 2
vlms:
  - {name: v1, model: vis-a, base_url: https://x/v1, api_key_env: VK}
routes:
  - match: {default: true}
    embedding: e1
    llm: l1
    llm_fallbacks: [l2]
    vlm: v1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert set(ps.llms) == {"l1", "l2"}
    assert set(ps.vlms) == {"v1"}
    l1, l2 = ps.llms["l1"], ps.llms["l2"]
    assert l1.provider == "openai_compatible"
    assert l1.timeout_s == 180.0 and l1.max_retries == 4  # 默认值
    assert l2.provider == "gemini_native"
    assert l2.timeout_s == 90.0 and l2.max_retries == 2
    r = ps.default_route
    assert r.llm == "l1"
    assert r.llm_fallbacks == ("l2",)
    assert r.vlm == "v1"
    assert r.vlm_fallbacks == ()
    # repr 必须脱敏 api_key
    assert "LK-secret" not in repr(l1)
    assert "***" in repr(l1) and "***" in repr(ps.vlms["v1"])


def test_v1_yaml_has_empty_llms_vlms(tmp_path, monkeypatch):
    """v1 文件不写 llms/vlms → 解析为空 dict，route.llm/vlm 为 None（向后兼容）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 1
embeddings:
  - {name: p1, model: m, dim: 8, base_url: https://x/v1, api_key_env: K}
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert ps.llms == {} and ps.vlms == {}
    assert ps.default_route.llm is None
    assert ps.default_route.vlm is None
    assert ps.default_route.llm_fallbacks == ()


def test_route_references_unknown_llm(tmp_path, monkeypatch):
    """route 引用不存在的 llm profile 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 2
embeddings:
  - {name: p1, model: m, dim: 8, base_url: https://x/v1, api_key_env: K}
routes:
  - match: {default: true}
    embedding: p1
    llm: ghost-llm
""",
    )
    with pytest.raises(ValueError, match=r"ghost-llm"):
        load_profiles(str(yaml_path))


def test_route_references_unknown_vlm_fallback(tmp_path, monkeypatch):
    """route 的 vlm_fallbacks 引用不存在的 vlm profile 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 2
embeddings:
  - {name: p1, model: m, dim: 8, base_url: https://x/v1, api_key_env: K}
vlms:
  - {name: v1, model: vis, base_url: https://x/v1, api_key_env: K}
routes:
  - match: {default: true}
    embedding: p1
    vlm: v1
    vlm_fallbacks: [ghost-vlm]
""",
    )
    with pytest.raises(ValueError, match=r"ghost-vlm"):
        load_profiles(str(yaml_path))


def test_route_references_unknown_reranker_fallback(tmp_path, monkeypatch):
    """route 的 reranker_fallbacks 引用不存在的 reranker 必须 raise。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 2
embeddings:
  - {name: p1, model: m, dim: 8, base_url: https://x/v1, api_key_env: K}
routes:
  - match: {default: true}
    embedding: p1
    reranker_fallbacks: [ghost-rr]
""",
    )
    with pytest.raises(ValueError, match=r"ghost-rr"):
        load_profiles(str(yaml_path))


def test_legacy_env_synthesizes_llm_and_vlm(monkeypatch):
    """无 YAML → 从 LLM_* / VLM_* env 合成 legacy-llm / legacy-vlm profile。"""
    _clear_legacy_env(monkeypatch)
    for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
              "VLM_MODEL", "VLM_BASE_URL", "VLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_MODEL", "my-chat")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("VLM_MODEL", "my-vision")
    ps = load_profiles()
    assert set(ps.llms) == {"legacy-llm"}
    assert set(ps.vlms) == {"legacy-vlm"}
    llm = ps.llms["legacy-llm"]
    assert llm.model == "my-chat"
    assert llm.base_url == "https://llm.test/v1"
    assert llm.api_key == "llm-key"
    vlm = ps.vlms["legacy-vlm"]
    assert vlm.model == "my-vision"
    # VLM_BASE_URL/API_KEY 缺省 → 回落到 legacy-llm
    assert vlm.base_url == "https://llm.test/v1"
    assert vlm.api_key == "llm-key"
    assert ps.default_route.llm == "legacy-llm"
    assert ps.default_route.vlm == "legacy-vlm"


def test_legacy_llm_base_url_falls_back_to_embedding(monkeypatch):
    """LLM_BASE_URL 缺失时回落到 EMBEDDING_BASE_URL（保留历史 env 兜底链）。"""
    _clear_legacy_env(monkeypatch)
    for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://shared.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "shared-key")
    ps = load_profiles()
    llm = ps.llms["legacy-llm"]
    assert llm.base_url == "https://shared.test/v1"
    assert llm.api_key == "shared-key"


def test_v2_version_accepted(tmp_path, monkeypatch):
    """version 2 显式被接受（不报 unsupported）。"""
    _clear_legacy_env(monkeypatch)
    monkeypatch.setenv("K", "v")
    yaml_path = _write_yaml(
        tmp_path,
        """
version: 2
embeddings:
  - {name: p1, model: m, dim: 8, base_url: https://x/v1, api_key_env: K}
routes:
  - match: {default: true}
    embedding: p1
""",
    )
    ps = load_profiles(str(yaml_path))
    assert "p1" in ps.embeddings
