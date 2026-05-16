from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


DEFAULT_CONTAINER = 'imac'


PatternMode = Literal['substring', 'prefix', 'glob']


class _WithModelOverride(BaseModel):
    """Mixin: optional per-request model overrides (Phase 1+2).

    用 mixin 而不是基类继承链，避免和 ContainerReq 的字段顺序冲突。
    Phase 1 真实生效的是 embedding_model（ingest 路径）；reranker_model / rerank
    字段先接受参数但 Phase 1 不做行为变更，Phase 2 才接到 LightRAG QueryParam。
    """

    embedding_model: str | None = Field(
        None,
        description=(
            "Override the route's default embedding profile for this request. "
            "Must be a profile name declared in profiles.yaml. Phase 1 supports "
            "this on ingest endpoints (warning: choice persists in LanceDB)."
        ),
    )
    reranker_model: str | None = Field(
        None,
        description='Override reranker profile. Phase 2 feature.',
    )
    rerank: bool | None = Field(
        None,
        description='Enable/disable reranker for this request. Defaults to route config. Phase 2 feature.',
    )


class SearchReq(_WithModelOverride):
    query: str = Field(..., min_length=1)
    topk: int = Field(default=5, ge=1, le=100)
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    containers: list[str] | None = Field(
        default=None,
        description='显式指定要搜索的容器列表；非空时优先级高于 container_pattern 与 container。',
    )
    container_pattern: str | None = Field(
        default=None,
        max_length=64,
        description='按 pattern_mode 模糊匹配容器名（大小写不敏感）。当 containers 为空时生效，优先级高于 container。',
    )
    pattern_mode: PatternMode = Field(
        default='substring',
        description='container_pattern 的匹配模式：substring（子串）/ prefix（前缀）/ glob（fnmatch）。',
    )
    timeout_s: int = Field(default=600, ge=1, le=1800)


class ContainerReq(_WithModelOverride):
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    timeout_s: int = Field(default=600, ge=1, le=1800)
    background: bool | None = None
    wait: bool = False


class IngestMemoryReq(ContainerReq):
    memory_dir: str | None = None
    archive_dir: str | None = None


class StructuredIngestReq(ContainerReq):
    input_path: str
    doc_type: str = 'structured_json'
    doc_id: str | None = None


class IngestObject(BaseModel):
    id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    title: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class ClientIngestReq(_WithModelOverride):
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    objects: list[IngestObject] = Field(..., min_length=1)
    auto_embed: bool = Field(default=True, description='Automatically trigger background embed after ingest')


class CommandResponse(BaseModel):
    command: list[str]
    code: int
    stdout: str = ''
    stderr: str = ''
    background: bool = False
    wait: bool = True
    pid: int | None = None
    status: str | None = None
    note: str | None = None


class SearchHit(BaseModel):
    score: float | None = None
    container: str | None = None
    taskId: str | None = None
    chunkId: str | None = None
    docType: str | None = None
    sourcePath: str | None = None
    section: str | None = None
    structuredPath: str | None = None
    title: str | None = None
    source: str | None = None
    text: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    status: Literal['ok', 'error']
    command: list[str]
    code: int
    query: str
    topk: int
    container: str
    containers: list[str] = Field(
        default_factory=list,
        description='本次实际命中的容器列表（解析 containers / container_pattern / container 之后）。',
    )
    per_container_status: dict[str, str] = Field(
        default_factory=dict,
        description='每个命中容器的执行状态：ok / not_initialized / error: <message>。',
    )
    initialized: bool
    message: str | None = None
    results: list[SearchHit]
    stdout: str
    stderr: str


class ModuleStatusResponse(BaseModel):
    enabled: bool
    ready: bool
    package_available: bool
    required_keys: list[str] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)


class ConfigurationGuide(BaseModel):
    configured: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    optional: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """公开健康端点 — LB-style 最小响应。

    诊断细节（容器列表、阈值数值、队列深度、配置 key、原始系统指标）请改用
    需鉴权的 /admin/system-health。本响应刻意不暴露任何具体数值、绝对路径、
    容器名或环境变量名，避免给匿名访问者积累指纹/侦察信息。
    """
    status: Literal['ok']
    service: str
    architecture: str
    build_flavor: Literal['lite', 'full']
    multimodal_capable: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    runtime_ready: dict[str, bool] = Field(default_factory=dict)
    accepting_ingest: bool = True
    worker_running: bool = False
    uptime_seconds: int = 0
    # 每维度压力标签（'ok' / 'pressure'），不暴露阈值或数值。
    # 客户端可据此提前退避；具体数值见 /admin/system-health。
    system_status: dict[str, str] = Field(default_factory=dict)
    # 已脱敏的可用性提示（不含数值/路径/容器名）。完整原文见 /admin/system-health。
    warnings: list[str] = Field(default_factory=list)


class ClientIngestResponse(BaseModel):
    container: str
    accepted: int
    stored_path: str
    stored_paths: list[str]
    index_hint: str


# --- 多模态 RAG 集成新增模型 ---


class OnboardingPromptResponse(BaseModel):
    id: str
    title: str
    prompt: str
    reason: str


class PairingAuthResponse(BaseModel):
    mode: Literal['api_key']
    endpoint: str
    api_key: str
    container: str
    accepted_headers: list[str] = Field(default_factory=list)
    token_transport: str
    config_path: str


class AgentOnboardingResponse(BaseModel):
    collect_from_user: list[OnboardingPromptResponse] = Field(default_factory=list)
    tell_user: list[str] = Field(default_factory=list)
    recommended_commands: list[str] = Field(default_factory=list)


class ConnectionTokenResponse(BaseModel):
    token: str
    endpoint: str
    container: str
    note: str
    pairing_auth: PairingAuthResponse
    agent_onboarding: AgentOnboardingResponse


class UpdateMemoryReq(BaseModel):
    text: str | None = None
    title: str | None = None
    source: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, str | int | float | bool | None] | None = None


class MemoryDeleteResponse(BaseModel):
    container: str
    id: str
    deleted: bool
    message: str


class MemoryUpdateResponse(BaseModel):
    container: str
    id: str
    updated: bool
    message: str
    index_hint: str


class ContainerListResponse(BaseModel):
    containers: list[str]
    count: int


class ContainerInfo(BaseModel):
    name: str
    objects: int
    indexed: bool
    last_modified: str | None = None


class ContainerListDetailedResponse(BaseModel):
    containers: list[ContainerInfo]
    count: int


class ContainerDeleteResponse(BaseModel):
    container: str
    deleted: bool
    message: str


class JobStatusResponse(BaseModel):
    pid: int
    running: bool
    exit_code: int | None = None
    message: str


class DocumentTextReq(_WithModelOverride):
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    text: str = Field(..., min_length=1)
    description: str | None = None


class QueryReq(_WithModelOverride):
    query: str = Field(..., min_length=1)
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    mode: str = "hybrid"
    top_k: int = Field(default=60, ge=1, le=500)


class QueryResponse(BaseModel):
    status: str
    query: str
    container: str
    answer: str
    mode: str
