from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


DEFAULT_CONTAINER = 'default'


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
    union: bool | None = Field(
        default=None,
        description=(
            '单 container 入参时是否自动 union 到 sibling 镜像（X + X_openai）。'
            'None=按 profiles.yaml 的 union_search_default 决定；True/False 显式覆盖。'
            '当指定 containers 或 container_pattern 时本字段被忽略（用户已显式控制）。'
        ),
    )
    per_container_timeout_s: float | None = Field(
        default=None,
        ge=0.5,
        le=30.0,
        description=(
            '单容器子查询超时上限（秒）。超时容器在 per_container_status 标记 timeout，'
            '不影响其余容器返回。None=默认 12.0s（容忍 subprocess cold-start；v0.12 in-process 化后可降回 3s）。'
        ),
    )


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
    vectorScore: float | None = Field(
        default=None,
        description='Original LanceDB vector distance before rerank. Smaller is better.',
    )
    rerankScore: float | None = Field(
        default=None,
        description='Reranker relevance score when /search rerank is applied. Larger is better.',
    )
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
    degraded: bool = Field(
        default=False,
        description='True 表示至少一个目标容器超时或失败，结果不完整但已尽力合并。',
    )
    union_applied: bool = Field(
        default=False,
        description='True 表示本次查询触发了 sibling _openai 镜像自动 union（双轨召回）。',
    )
    rerank_applied: bool = Field(
        default=False,
        description='True 表示本次 /search 结果已经经过 reranker 重排。',
    )
    reranker: str | None = Field(
        default=None,
        description='实际用于 /search 重排的 reranker profile 名称。',
    )


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
    index_state: str | None = Field(
        default=None,
        description='容器索引状态机：fresh / indexing / backlog / quota_blocked / error / stale / unknown。',
    )


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


# --- 容器索引状态机 / embedding backlog 响应模型 ---


class IndexStatusResponse(BaseModel):
    """单容器索引状态机视图。

    ``state`` 由 ``index_state.compute_index_state`` 实时推导（fresh / indexing /
    backlog / quota_blocked / error / stale / unknown），不读缓存字段，避免投影
    与真实状态失同步。计数与时间戳来自 ``container_index_state`` + ``embed_backlog``。
    """
    container: str
    state: str = Field(
        ...,
        description='fresh / indexing / backlog / quota_blocked / error / stale / unknown',
    )
    total_objects: int = 0
    embedded_objects: int = 0
    backlog_active: int = Field(
        default=0,
        description='backlog 中 waiting + retrying 的 chunk 数（待静默重试）。',
    )
    backlog_counts: dict[str, int] = Field(
        default_factory=dict,
        description='backlog 各状态计数：waiting / retrying / resolved / dead。',
    )
    dead_count: int = Field(
        default=0,
        description='永久失败（dead-letter）的 chunk 数 —— 需人工介入。',
    )
    job_running: bool = Field(
        default=False,
        description='该容器当前是否有 embed 类 job 处于 pending / running。',
    )
    next_retry_at: int | None = Field(
        default=None,
        description='最近一个待重试 chunk 的下次重试时间（unix ts）。',
    )
    last_error_class: str | None = Field(
        default=None,
        description='最近一次失败的错误类别：quota / timeout / transient。',
    )
    last_embed_ok_at: int | None = None
    last_embed_attempt_at: int | None = None


class IndexStatusListResponse(BaseModel):
    containers: list[IndexStatusResponse] = Field(default_factory=list)
    count: int = 0


class BacklogItemResponse(BaseModel):
    """单条 embedding backlog 明细。``last_error`` 已截断防止响应体被长 traceback 撑大。"""
    chunk_id: str
    content_hash: str | None = None
    error_class: str
    attempts: int = 0
    first_failed_at: int = 0
    last_attempt_at: int = 0
    next_retry_at: int = 0
    last_error: str | None = None
    status: str
    resolved_at: int | None = None


class BacklogListResponse(BaseModel):
    container: str
    count: int = 0
    active: int = Field(default=0, description='waiting + retrying 的 chunk 数。')
    dead: int = Field(default=0, description='永久失败的 chunk 数。')
    items: list[BacklogItemResponse] = Field(default_factory=list)


class DocumentTextReq(_WithModelOverride):
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    text: str = Field(..., min_length=1)
    description: str | None = None


class QueryReq(_WithModelOverride):
    query: str = Field(..., min_length=1)
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    mode: str = "hybrid"
    top_k: int = Field(default=60, ge=1, le=500)
    chunk_top_k: int | None = Field(
        default=None,
        ge=1,
        le=500,
        description=(
            'Number of chunks to keep after rerank (LightRAG QueryParam.chunk_top_k). '
            'Defaults to route.chunk_top_k when rerank enabled. Phase 2 feature.'
        ),
    )


class QueryResponse(BaseModel):
    status: str
    query: str
    container: str
    answer: str
    mode: str


class ContainerMetadataPayload(BaseModel):
    """container_metadata 表的 upsert 请求体（所有字段可选 / partial update）。"""

    description: str | None = None
    tags: list[str] | None = None
    scope: str | None = Field(
        default=None,
        description='凭证体系：team | personal | shared',
    )
    entity: str | None = Field(
        default=None,
        description='项目名 / 设备名 / agent 名',
    )
    purpose: str | None = Field(
        default=None,
        description='eng | runbook | playbook | personal | active | archive | prime',
    )
    owner: str | None = None
    policy: dict | None = Field(
        default=None,
        description='策略字典（retention_days / max_objects / auto_reembed 等）',
    )
    archived_at: str | None = Field(
        default=None,
        description='ISO8601 归档时间；None 表示未归档',
    )
