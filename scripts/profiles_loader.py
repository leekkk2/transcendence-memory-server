#!/usr/bin/env python3
"""Multi-embedding profile loader.

负责把 `config/profiles.yaml` 解析成 frozen dataclass 集合，或在缺失 YAML 时
从 legacy env (`EMBEDDING_*`) 合成一个等价的 `legacy` profile，保证旧部署零
配置兼容。所有 secret 通过 `api_key_env` 间接引用环境变量，YAML 永远不直接
存裸 key（防止意外提交泄漏）。

校验内容（在 load 末尾统一跑）：
1. 每个 route.embedding/embedding_fallbacks 必须在 embeddings dict 里
2. fallback 的 dim 必须 == primary dim（否则跨 dim 切换没意义）
3. 必须恰好 1 个 default route
4. embeddings/rerankers 字典 key 唯一
5. api_key_env 在 os.environ 里能取到非空值
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 默认值集中放，避免硬编码到多个地方
_DEFAULT_EMBED_MAX_TOKEN = 8192
_DEFAULT_EMBED_TIMEOUT_S = 60.0
_DEFAULT_EMBED_MAX_RETRIES = 3
_DEFAULT_RERANKER_TIMEOUT_S = 30.0
_DEFAULT_RERANKER_MIN_SCORE = 0.0
_DEFAULT_CHUNK_TOP_K = 30
_DEFAULT_TOP_K = 8
# LLM / VLM 默认值 —— 生成类调用比 embedding 慢得多，timeout / retry 更宽。
_DEFAULT_LLM_TIMEOUT_S = 180.0
_DEFAULT_LLM_MAX_RETRIES = 4
_LEGACY_DEFAULT_MODEL = "text-embedding-3-small"
_LEGACY_DEFAULT_DIM = 1024
_LEGACY_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_LEGACY_DEFAULT_LLM_MODEL = "gemini-2.5-flash"
# v1：仅 embedding + reranker；v2：新增 llms / vlms 节点与 route 的 LLM/VLM 字段。
# 两版都接受 —— v1 文件不写新节点即解析为空 dict，向后兼容。
_SUPPORTED_VERSIONS = frozenset({1, 2})


@dataclass(frozen=True)
class EmbeddingProfile:
    """单个 embedding provider 配置。frozen 防止运行期被改。"""

    name: str
    provider: str  # "openai_compatible"（/v1/embeddings 纯文本）| "gemini_native"（:embedContent 原生多模态）
    model: str
    dim: int
    base_url: str
    api_key: str  # 已解析的真实 key（不是 env 名）
    max_token_size: int = _DEFAULT_EMBED_MAX_TOKEN
    request_dim: int | None = None  # 设为 N 时 POST body 加 `dimensions=N` (Matryoshka)
    timeout_s: float = _DEFAULT_EMBED_TIMEOUT_S
    max_retries: int = _DEFAULT_EMBED_MAX_RETRIES

    def __repr__(self) -> str:  # 强制 redact，防止日志/调试输出泄漏 key
        return (
            f"EmbeddingProfile(name={self.name!r}, model={self.model!r}, "
            f"dim={self.dim}, base_url={self.base_url!r}, api_key='***')"
        )


@dataclass(frozen=True)
class RerankerProfile:
    """单个 reranker provider 配置。"""

    name: str
    provider: str  # "cohere_compatible"
    model: str
    base_url: str
    api_key: str
    timeout_s: float = _DEFAULT_RERANKER_TIMEOUT_S
    min_score: float = _DEFAULT_RERANKER_MIN_SCORE

    def __repr__(self) -> str:
        return (
            f"RerankerProfile(name={self.name!r}, model={self.model!r}, "
            f"base_url={self.base_url!r}, api_key='***')"
        )


@dataclass(frozen=True)
class LLMProfile:
    """单个 LLM（chat / 生成）provider 配置。frozen 防止运行期被改。

    LLM 无 dim 概念 —— 不受 LanceDB 向量维度锁约束，故 fallback 链可跨任意
    模型，无需同维校验（区别于 embedding）。
    """

    name: str
    model: str
    base_url: str
    api_key: str  # 已解析的真实 key（不是 env 名）
    provider: str = "openai_compatible"  # 另支持 "gemini_native"
    timeout_s: float = _DEFAULT_LLM_TIMEOUT_S
    max_retries: int = _DEFAULT_LLM_MAX_RETRIES

    def __repr__(self) -> str:  # 强制 redact，防止日志 / 调试输出泄漏 key
        return (
            f"LLMProfile(name={self.name!r}, model={self.model!r}, "
            f"provider={self.provider!r}, base_url={self.base_url!r}, "
            f"api_key='***')"
        )


@dataclass(frozen=True)
class VLMProfile:
    """单个 VLM（视觉 / 多模态生成）provider 配置。

    与 LLMProfile 字段一致，单独成类以便 schema 校验时区分两类引用池。
    同样无 dim 校验。
    """

    name: str
    model: str
    base_url: str
    api_key: str
    provider: str = "openai_compatible"  # 另支持 "gemini_native"
    timeout_s: float = _DEFAULT_LLM_TIMEOUT_S
    max_retries: int = _DEFAULT_LLM_MAX_RETRIES

    def __repr__(self) -> str:
        return (
            f"VLMProfile(name={self.name!r}, model={self.model!r}, "
            f"provider={self.provider!r}, base_url={self.base_url!r}, "
            f"api_key='***')"
        )


@dataclass(frozen=True)
class Route:
    """路由：container → embedding / LLM / VLM / reranker 组合 + rerank 调参。

    LLM / VLM / reranker_fallbacks 全部带默认值 —— v1 profiles.yaml 不写这些
    字段时解析为 None / 空元组，向后兼容。
    """

    embedding: str
    embedding_fallbacks: tuple[str, ...] = ()
    reranker: str | None = None
    rerank_enabled: bool = False
    chunk_top_k: int = _DEFAULT_CHUNK_TOP_K
    top_k: int = _DEFAULT_TOP_K
    llm: str | None = None
    llm_fallbacks: tuple[str, ...] = ()
    vlm: str | None = None
    vlm_fallbacks: tuple[str, ...] = ()
    reranker_fallbacks: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileSet:
    """加载后的全量 profile + 路由表。

    `routes` 是按文件顺序的 (matcher_dict, Route) 列表，resolve 时按顺序匹配。
    `default_route` 单独拎出来，避免每次 resolve 都遍历找 default。
    `union_search_default` 是 v0.11.0 引入的顶层开关：true 时 /search 单 container
    入参会自动追加 sibling _openai 镜像做 union（双轨召回）；缺省 false 保持旧行为。
    """

    embeddings: dict[str, EmbeddingProfile]
    rerankers: dict[str, RerankerProfile]
    routes: list[tuple[dict[str, Any], Route]] = field(default_factory=list)
    default_route: Route | None = None
    union_search_default: bool = False
    # v2 新增：LLM / VLM profile 池。v1 文件 / legacy env 下为对应合成结果或空。
    llms: dict[str, LLMProfile] = field(default_factory=dict)
    vlms: dict[str, VLMProfile] = field(default_factory=dict)


def _resolve_yaml_path(yaml_path: str | None) -> Path | None:
    """按优先级寻找 YAML：参数 > env TM_PROFILES_FILE > $WORKSPACE/config/profiles.yaml。"""
    if yaml_path:
        p = Path(yaml_path)
        return p if p.exists() else None
    env_path = os.environ.get("TM_PROFILES_FILE")
    if env_path:
        p = Path(env_path)
        return p if p.exists() else None
    workspace = os.environ.get("WORKSPACE")
    if workspace:
        p = Path(workspace) / "config" / "profiles.yaml"
        if p.exists():
            return p
    return None


def _read_api_key(env_name: str, profile_name: str) -> str:
    """从环境变量取 api key；缺失或空字符串都视为缺失，明确告知是哪个 profile。"""
    value = os.environ.get(env_name, "")
    if not value:
        raise ValueError(
            f"profile {profile_name!r} requires env var {env_name!r} "
            "but it is missing or empty"
        )
    return value


def _parse_embedding(item: dict[str, Any]) -> EmbeddingProfile:
    """单条 embedding YAML 节点 → EmbeddingProfile。缺字段直接 KeyError 暴露问题。"""
    name = item["name"]
    api_key = _read_api_key(item["api_key_env"], name)
    return EmbeddingProfile(
        name=name,
        provider=item.get("provider", "openai_compatible"),
        model=item["model"],
        dim=int(item["dim"]),
        base_url=item["base_url"],
        api_key=api_key,
        max_token_size=int(item.get("max_token_size", _DEFAULT_EMBED_MAX_TOKEN)),
        request_dim=(int(item["request_dim"]) if item.get("request_dim") is not None else None),
        timeout_s=float(item.get("timeout_s", _DEFAULT_EMBED_TIMEOUT_S)),
        max_retries=int(item.get("max_retries", _DEFAULT_EMBED_MAX_RETRIES)),
    )


def _parse_reranker(item: dict[str, Any]) -> RerankerProfile:
    name = item["name"]
    api_key = _read_api_key(item["api_key_env"], name)
    return RerankerProfile(
        name=name,
        provider=item.get("provider", "cohere_compatible"),
        model=item["model"],
        base_url=item["base_url"],
        api_key=api_key,
        timeout_s=float(item.get("timeout_s", _DEFAULT_RERANKER_TIMEOUT_S)),
        min_score=float(item.get("min_score", _DEFAULT_RERANKER_MIN_SCORE)),
    )


def _parse_llm(item: dict[str, Any]) -> LLMProfile:
    """单条 llm YAML 节点 → LLMProfile。缺字段直接 KeyError 暴露问题。"""
    name = item["name"]
    api_key = _read_api_key(item["api_key_env"], name)
    return LLMProfile(
        name=name,
        model=item["model"],
        base_url=item["base_url"],
        api_key=api_key,
        provider=item.get("provider", "openai_compatible"),
        timeout_s=float(item.get("timeout_s", _DEFAULT_LLM_TIMEOUT_S)),
        max_retries=int(item.get("max_retries", _DEFAULT_LLM_MAX_RETRIES)),
    )


def _parse_vlm(item: dict[str, Any]) -> VLMProfile:
    """单条 vlm YAML 节点 → VLMProfile。缺字段直接 KeyError 暴露问题。"""
    name = item["name"]
    api_key = _read_api_key(item["api_key_env"], name)
    return VLMProfile(
        name=name,
        model=item["model"],
        base_url=item["base_url"],
        api_key=api_key,
        provider=item.get("provider", "openai_compatible"),
        timeout_s=float(item.get("timeout_s", _DEFAULT_LLM_TIMEOUT_S)),
        max_retries=int(item.get("max_retries", _DEFAULT_LLM_MAX_RETRIES)),
    )


def _parse_route(item: dict[str, Any]) -> tuple[dict[str, Any], Route]:
    """单条 route YAML 节点 → (matcher_dict, Route)。

    rerank 子字典支持 {enabled, chunk_top_k, top_k}，缺省走 dataclass 默认值。
    llm / vlm / reranker_fallbacks 缺省时解析为 None / 空元组（v1 兼容）。
    """
    matcher = dict(item.get("match", {}))
    rerank_cfg = item.get("rerank") or {}
    route = Route(
        embedding=item["embedding"],
        embedding_fallbacks=tuple(item.get("embedding_fallbacks") or ()),
        reranker=item.get("reranker"),
        rerank_enabled=bool(rerank_cfg.get("enabled", False)),
        chunk_top_k=int(rerank_cfg.get("chunk_top_k", _DEFAULT_CHUNK_TOP_K)),
        top_k=int(rerank_cfg.get("top_k", _DEFAULT_TOP_K)),
        llm=item.get("llm"),
        llm_fallbacks=tuple(item.get("llm_fallbacks") or ()),
        vlm=item.get("vlm"),
        vlm_fallbacks=tuple(item.get("vlm_fallbacks") or ()),
        reranker_fallbacks=tuple(item.get("reranker_fallbacks") or ()),
    )
    return matcher, route


def _validate_embedding_refs(r: Route, embeddings: dict[str, EmbeddingProfile]) -> None:
    """校验 route 的 embedding primary + fallbacks：引用存在，且 fallback 同 dim。

    embedding fallback 强制同 dim —— LanceDB 表的向量维度在建表时锁定，跨 dim
    切换会让旧向量与新向量无法同表检索。LLM / VLM 无此约束。
    """
    if r.embedding not in embeddings:
        raise ValueError(
            f"route references unknown embedding profile {r.embedding!r}"
        )
    primary_dim = embeddings[r.embedding].dim
    for fb in r.embedding_fallbacks:
        if fb not in embeddings:
            raise ValueError(
                f"route fallback references unknown embedding profile {fb!r}"
            )
        if embeddings[fb].dim != primary_dim:
            raise ValueError(
                f"fallback {fb!r} dim={embeddings[fb].dim} 与 primary "
                f"{r.embedding!r} dim={primary_dim} 不一致，无法 fallback"
            )


def _validate_model_refs(r: Route, ps: ProfileSet) -> None:
    """校验 route 的 LLM / VLM / reranker primary + fallbacks 引用都存在。

    三类均**无 dim 校验** —— 只有 embedding 受 LanceDB 向量维度锁约束，生成类
    与 reranker 的 fallback 链可跨任意模型。None（未配置）跳过。
    """
    for kind, names, pool in (
        ("llm", (r.llm, *r.llm_fallbacks), ps.llms),
        ("vlm", (r.vlm, *r.vlm_fallbacks), ps.vlms),
        ("reranker", (r.reranker, *r.reranker_fallbacks), ps.rerankers),
    ):
        for name in names:
            if name is not None and name not in pool:
                raise ValueError(
                    f"route references unknown {kind} profile {name!r}"
                )


def _validate(ps: ProfileSet) -> None:
    """统一在加载末尾跑一次：发现任何配置不一致就 raise，启动期就失败而不是运行期。"""
    # default 必须恰好 1 个（_load_yaml 已强制 default_route 非空，_load_legacy_env
    # 也会构造，所以这里只校验非 None 即可）
    if ps.default_route is None:
        raise ValueError("ProfileSet must have exactly one default route")
    all_routes = [r for _, r in ps.routes] + [ps.default_route]
    for r in all_routes:
        _validate_embedding_refs(r, ps.embeddings)
        _validate_model_refs(r, ps)


def _parse_named_section(
    doc: dict[str, Any], key: str, parser: Any, kind: str
) -> dict[str, Any]:
    """通用 YAML 列表节点解析 —— 逐条 parse 并查重名。

    v1 文件不写 ``llms`` / ``vlms`` 节点时 ``doc.get(key)`` 为 None → 返回空
    dict，向后兼容。
    """
    out: dict[str, Any] = {}
    for item in doc.get(key, []) or []:
        prof = parser(item)
        if prof.name in out:
            raise ValueError(f"duplicate {kind} profile name {prof.name!r}")
        out[prof.name] = prof
    return out


def _load_yaml(path: Path) -> ProfileSet:
    """读 YAML 文件并构造 ProfileSet。重复 name、版本不符、无 default 都视为致命。"""
    with path.open("r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    version = doc.get("version", 1)
    if version not in _SUPPORTED_VERSIONS:
        raise ValueError(
            f"profiles.yaml version {version} not supported, "
            f"expect one of {sorted(_SUPPORTED_VERSIONS)}"
        )

    embeddings = _parse_named_section(doc, "embeddings", _parse_embedding, "embedding")
    rerankers = _parse_named_section(doc, "rerankers", _parse_reranker, "reranker")
    llms = _parse_named_section(doc, "llms", _parse_llm, "llm")
    vlms = _parse_named_section(doc, "vlms", _parse_vlm, "vlm")

    routes: list[tuple[dict[str, Any], Route]] = []
    default_route: Route | None = None
    for item in doc.get("routes", []) or []:
        matcher, route = _parse_route(item)
        if matcher.get("default") is True:
            if default_route is not None:
                raise ValueError("multiple default routes defined — expect exactly one")
            default_route = route
        else:
            routes.append((matcher, route))

    if default_route is None:
        raise ValueError("profiles.yaml must define exactly one route with {default: true}")

    # v0.11.0：顶层 union_search_default 字段，缺省 false 保持向后兼容
    union_default = bool(doc.get("union_search_default", False))

    ps = ProfileSet(
        embeddings=embeddings,
        rerankers=rerankers,
        routes=routes,
        default_route=default_route,
        union_search_default=union_default,
        llms=llms,
        vlms=vlms,
    )
    _validate(ps)
    return ps


def _legacy_llm_profile() -> LLMProfile:
    """从 LLM_* env 合成 legacy-llm profile。

    base_url / api_key 保留对 EMBEDDING_* 的历史兜底链 —— 很多旧部署只配了
    一对 base / key 给 embedding，LLM 复用同一对。legacy 模式 api_key 缺失
    不抛（与 embedding legacy 一致），保持旧部署零配置可启动。
    """
    base = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("EMBEDDING_BASE_URL")
        or os.environ.get("EMBEDDINGS_BASE_URL")
        or _LEGACY_DEFAULT_BASE_URL
    )
    key = os.environ.get("LLM_API_KEY") or os.environ.get("EMBEDDING_API_KEY", "")
    return LLMProfile(
        name="legacy-llm",
        model=os.environ.get("LLM_MODEL", _LEGACY_DEFAULT_LLM_MODEL),
        base_url=base,
        api_key=key,
    )


def _legacy_vlm_profile(llm: LLMProfile) -> VLMProfile:
    """从 VLM_* env 合成 legacy-vlm profile。

    缺省回落到 legacy-llm 的 model / base_url / api_key —— 与历史多模态引擎
    的 VLM→LLM env 兜底链一致（旧部署常只配 LLM 一套）。
    """
    return VLMProfile(
        name="legacy-vlm",
        model=os.environ.get("VLM_MODEL") or llm.model,
        base_url=os.environ.get("VLM_BASE_URL") or llm.base_url,
        api_key=os.environ.get("VLM_API_KEY") or llm.api_key,
    )


def _load_legacy_env() -> ProfileSet:
    """无 YAML 时从 *_* env 合成 legacy embedding / llm / vlm profile + default。"""
    model = os.environ.get("EMBEDDING_MODEL", _LEGACY_DEFAULT_MODEL)
    dim = int(os.environ.get("EMBEDDING_DIM", str(_LEGACY_DEFAULT_DIM)))
    base = (
        os.environ.get("EMBEDDING_BASE_URL")
        or os.environ.get("EMBEDDINGS_BASE_URL")
        or _LEGACY_DEFAULT_BASE_URL
    )
    # legacy 模式下 api_key 缺失不抛 — 老部署允许空 key 启动（即使后续请求会 401），
    # 保持 v0.6 行为完全等价
    key = os.environ.get("EMBEDDING_API_KEY", "")
    profile = EmbeddingProfile(
        name="legacy",
        provider="openai_compatible",
        model=model,
        dim=dim,
        base_url=base,
        api_key=key,
    )
    legacy_llm = _legacy_llm_profile()
    legacy_vlm = _legacy_vlm_profile(legacy_llm)
    default_route = Route(embedding="legacy", llm="legacy-llm", vlm="legacy-vlm")
    ps = ProfileSet(
        embeddings={"legacy": profile},
        rerankers={},
        routes=[],
        default_route=default_route,
        llms={"legacy-llm": legacy_llm},
        vlms={"legacy-vlm": legacy_vlm},
    )
    # legacy 路径也跑 _validate，但跳过 api_key 校验（已在 _read_api_key 之外）
    _validate(ps)
    return ps


def load_profiles(yaml_path: str | None = None) -> ProfileSet:
    """加载 profile 集合。

    YAML 路径解析顺序：
      1. 参数 `yaml_path`
      2. env `TM_PROFILES_FILE`
      3. $WORKSPACE/config/profiles.yaml
      4. 都不存在 → 从 legacy env 合成

    Args:
        yaml_path: 显式指定 YAML 路径，None 时走 env / workspace 默认。

    Returns:
        ProfileSet：embeddings + rerankers + 路由表 + default route，已校验。

    Raises:
        ValueError: 任何配置不一致（缺字段、dim 不匹配、无 default、缺 env 等）。
    """
    resolved = _resolve_yaml_path(yaml_path)
    if resolved is None:
        return _load_legacy_env()
    return _load_yaml(resolved)
