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
            '不影响其余容器返回。None=按 profiles.yaml 的 union_per_container_timeout_s（默认 30.0s，'
            '容忍 subprocess cold-start；v0.12 in-process 化后可降回 3s）。'
        ),
    )
    score_threshold: float | None = Field(
        default=None,
        description=(
            'score-gate：丢弃 score（L2 距离，越小越相关）> 该上界或 None 的 hit。'
            'None=用 profiles.yaml 的 similarity_threshold（默认关闭）；≤0=显式关闭。'
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
    # P4: 行号（lineStart/lineEnd）存于 metadata JSON（无新 LanceDB 列），由 server 的
    # _meta_line 从 metadata 投影到 Citation.lineStart/lineEnd —— 故 SearchHit 不设顶层行号字段。


class Citation(BaseModel):
    """信源溯源条目 —— 由 /search 命中的 hit 投影，供 Agent 直接引用出处。

    score 沿用 LanceDB L2 距离语义（越小越相关，非相似度）。section / container
    缺失时为 None。lineStart/lineEnd（P4）指向 sourcePath 中的 1-based 行范围，
    老 chunk（未记录行号）为 None。
    """

    chunkId: str | None = None
    sourcePath: str | None = None
    section: str | None = None
    score: float | None = None
    container: str | None = None
    lineStart: int | None = None
    lineEnd: int | None = None


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
    is_degraded: bool = Field(
        default=False,
        description='degraded 的 Agent 友好别名（同值双写）。部分容器失败但仍有结果时为 True。',
    )
    fallback_source: str | None = Field(
        default=None,
        description="优雅降级来源标记：部分容器成功时为 'partial_containers'，否则 None。",
    )
    citations: list[Citation] | None = Field(
        default=None,
        description='信源溯源数组（citation_enabled 时由 results 投影；否则 None）。',
    )
    blocked_low_score: int = Field(
        default=0,
        description='被 score-gate（距离上界）拦截丢弃的 hit 数。0 表示未启用或无拦截。',
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
    fallback_rendered: str | None = Field(
        default=None,
        description=(
            'P4: 当 score-gate 全拦（merged 清空）或全容器降级且配置了'
            ' config:rag:fallback_template 时渲染的结构化拦截体；未配置模板时为 None'
            '（行为与 P4 前逐字节一致）。'
        ),
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


class MemoryListItem(BaseModel):
    """memory_objects.jsonl 单行的列表投影（dashboard 浏览用，不含向量）。"""

    id: str | None = None
    title: str | None = None
    text: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    createdAt: int | None = None
    updatedAt: int | None = None
    storedAt: int | None = None


class MemoryListResponse(BaseModel):
    """GET /containers/{container}/memories 分页响应。

    limit 为 None 表示调用方未要求分页（全量返回，向后兼容语义）；total 恒为
    容器内对象总数，供客户端渲染「已加载 N / 共 M」。
    """

    container: str
    total: int
    limit: int | None = None
    offset: int = 0
    items: list[MemoryListItem] = Field(default_factory=list)


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
    score_threshold: float | None = Field(
        default=None,
        description=(
            'score-gate：top1 chunk 的 score（L2 距离）> 该上界或未初始化时直接返回 '
            "status='score_gated'，不调 LLM。None=用 profiles 默认（默认关闭）；≤0=显式关闭。"
        ),
    )


class QueryResponse(BaseModel):
    status: str
    query: str
    container: str
    answer: str
    mode: str
    top_score: float | None = Field(
        default=None,
        description='score-gate 命中时透出的 top1 chunk 距离（L2，越小越相关）。',
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description=(
            'P4: LLM 答案溯源数组。仅在 query_citation_enabled 且 answer 含可解析的引用'
            ' marker 并能映射到本次检索 chunk 时回填；否则为空（不改 answer 文本）。'
            ' 老客户端忽略未知字段。'
        ),
    )
    fallback_rendered: str | None = Field(
        default=None,
        description=(
            'P4: 当 score_gated / not_initialized 且配置了 config:rag:fallback_template 时'
            ' 渲染的结构化拦截体；未配置模板时为 None（行为与 P4 前逐字节一致）。'
        ),
    )


# ---------------------------------------------------------------------------
# Usage analytics response models (v0.17 — /admin/usage/*).
# ---------------------------------------------------------------------------


class UsageTopEndpoint(BaseModel):
    path: str
    calls: int
    p95: int = 0


class UsageSummaryResponse(BaseModel):
    window: str
    total_calls: int = 0
    total_errors: int = 0
    error_rate: float = 0.0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    active_containers: int = 0
    active_api_keys: int = 0
    top_endpoints: list[UsageTopEndpoint] = Field(default_factory=list)


class UsageEndpointRow(BaseModel):
    path: str
    calls: int = 0
    errors: int = 0
    p50_latency_ms: int = 0
    p95_latency_ms: int = 0
    distinct_containers: int = 0
    last_called_at: int | None = None


class UsageColdEndpoint(BaseModel):
    path: str
    calls: int = 0
    last_called_at: int | None = None


class UsageEndpointsResponse(BaseModel):
    window: str
    sort: Literal['calls', 'errors', 'p95'] = 'calls'
    rows: list[UsageEndpointRow] = Field(default_factory=list)
    cold_endpoints: list[UsageColdEndpoint] = Field(default_factory=list)


class UsageContainerRow(BaseModel):
    container: str
    calls: int = 0
    search_calls: int = 0
    ingest_calls: int = 0
    embed_calls: int = 0
    last_active: int | None = None


class UsageIdleContainer(BaseModel):
    container: str
    calls: int = 0
    memory_count: int = 0
    last_active: int | None = None


class UsageContainersResponse(BaseModel):
    window: str
    rows: list[UsageContainerRow] = Field(default_factory=list)
    idle_containers: list[UsageIdleContainer] = Field(default_factory=list)


class UsageTimeseriesPoint(BaseModel):
    ts: int
    calls: int = 0
    errors: int = 0
    p95: int = 0


class UsageTimeseriesResponse(BaseModel):
    path: str
    window: str
    bucket: Literal['5m', '1h', '1d']
    points: list[UsageTimeseriesPoint] = Field(default_factory=list)


class UsageCleanupRequest(BaseModel):
    retention_days: int = Field(default=30, ge=1, le=3650)


class UsageCleanupResponse(BaseModel):
    deleted_rows: int = 0
    kept_rows: int = 0


# ── Token usage / cost (blueprint P3 §7) ────────────────────────────────────


class TokenUsageDimensionRow(BaseModel):
    """One bucket of a single dimension (by_model / by_task_type / by_agent)."""

    key: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class TokenUsageTotals(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Live (in-flight, not-yet-flushed) total overlaid from Redis when available.
    live_total_tokens: int = 0


class TokenUsageResponse(BaseModel):
    window: str
    by_model: list[TokenUsageDimensionRow] = Field(default_factory=list)
    by_task_type: list[TokenUsageDimensionRow] = Field(default_factory=list)
    by_agent: list[TokenUsageDimensionRow] = Field(default_factory=list)
    totals: TokenUsageTotals = Field(default_factory=TokenUsageTotals)


# ── Runtime config center (blueprint P2 · GET/PUT /admin/config) ────────────
# Read/write models for the Dashboard "Config" view. They mirror config_store's
# read-only describe_key() output and feed config_store.set() on write. The
# sensitive-masking contract (api_keys:* never echo a value, only `configured`)
# is enforced server-side in config_store — these models just carry it through.


class ConfigItem(BaseModel):
    """One known config key as surfaced to the Dashboard.

    `value` is the current EFFECTIVE value (override if set, else `default`) for
    non-sensitive keys. For sensitive keys (api_keys:*) `value` is always None and
    `configured` tells the UI whether a secret has been persisted (so the input
    can render as a write-only "•••• set" affordance) — the secret is NEVER
    echoed, mirroring /admin/profiles' `api_key_configured`.
    """

    key: str = Field(..., description='Full config key, e.g. config:rag:similarity_threshold')
    module: str = Field(..., description='UI grouping derived from the key prefix: rag / model / token / …')
    type: Literal['int', 'float', 'bool', 'str', 'json'] = Field(
        ..., description='Coerced value type — drives the right input widget client-side.'
    )
    value: object | None = Field(
        default=None,
        description='Effective typed value (override if set, else default). Always null for sensitive keys.',
    )
    is_override: bool = Field(
        default=False,
        description='True iff a persisted override row exists (value differs from the registered default).',
    )
    default: object | None = Field(
        default=None,
        description='Registered no-override default the request path falls back to (sentinel for opt-in keys).',
    )
    configured: bool | None = Field(
        default=None,
        description='Sensitive keys only: whether a non-empty secret has been persisted. Null for non-sensitive keys.',
    )
    group: str | None = Field(
        default=None,
        description='Dashboard ConfigField grouping label (P6); falls back to module when unset.',
    )
    label: str | None = Field(
        default=None,
        description='Human-readable field label (P6); falls back to the key tail when unset.',
    )
    description: str | None = Field(
        default=None,
        description='User-facing one-line helper text explaining what the knob does; null when unregistered.',
    )


class ConfigListResponse(BaseModel):
    """Body for ``GET /admin/config`` — every known key in registry order."""

    items: list[ConfigItem] = Field(default_factory=list)
    count: int = 0


class ConfigUpdate(BaseModel):
    """One key/value pair to persist via ``PUT /admin/config``.

    `value` accepts the raw client value (str/int/float/bool/None); config_store.set
    does all coercion + the known-key / HR-9 base_url host-pin validation. Passing
    None (or empty string for the opt-in *_or_none keys) clears the override back to
    the registered default.
    """

    key: str = Field(..., min_length=1, description='Full config key; must be a registered KNOWN_CONFIG key.')
    value: object | None = Field(
        default=None,
        description='New value (raw; coerced + validated by config_store.set). None clears the override.',
    )


class ConfigUpdateRequest(BaseModel):
    """Body for ``PUT /admin/config`` — single or batch update."""

    updates: list[ConfigUpdate] = Field(
        ..., min_length=1, description='One or more key/value updates applied in order.'
    )


class ConfigUpdateResult(BaseModel):
    """Per-key outcome of a ``PUT /admin/config`` update.

    `ok=False` with `rejected_reason` covers an unknown key, a malformed value, an
    HR-9 base_url host-pin violation, or a DB write failure — config_store.set
    returns a single bool, so the reason is inferred from the rejection class
    without echoing the offending value (which may be sensitive).
    """

    key: str
    ok: bool
    rejected_reason: str | None = Field(
        default=None,
        description='Set only when ok=False, one of: unknown_key / rejected_base_url_host / invalid_value_or_persist_failed.',
    )


class ConfigUpdateResponse(BaseModel):
    """Body for ``PUT /admin/config`` — per-key results, registry-input order."""

    results: list[ConfigUpdateResult] = Field(default_factory=list)
    applied: int = Field(default=0, description='Count of updates that succeeded (ok=True).')
    rejected: int = Field(default=0, description='Count of updates that failed (ok=False).')


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


# ---------------------------------------------------------------------------
# Admin dashboard (`/admin/ui/*`) request / response models.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Body for ``POST /admin/ui/login``.

    The only thing the dashboard ships at login time is the api key — every
    other property (IP, UA) is read off the request directly by the server,
    not echoed back from the client where it could be spoofed.
    """

    api_key: str = Field(..., description='RAG_API_KEY plaintext; never stored — only its sha256 hash')


class SessionInfoResponse(BaseModel):
    """Body for ``GET /admin/ui/me``.

    Returns nothing sensitive — only the truncated hash (first 12 chars) so the
    dashboard can show a "logged in as …" hint without ever surfacing the raw
    api key in the SPA, plus the absolute expiry so the client can show a
    countdown / proactively re-login before a deep-link 401 fires.
    """

    api_key_hash: str = Field(..., description='First 12 chars of sha256(api_key) — display only')
    expires_at: int = Field(..., description='Unix timestamp at which this session lapses')
    env: str = Field(default='dev', description='TM_ENV — drives the topbar badge colour (prod/staging/dev)')


# ---------------------------------------------------------------------------
# Dreaming system (blueprint P6, §A7) — GET /admin/dreaming/status,
# POST /admin/dreaming/trigger. All fields default / optional so the models
# stay additive and never reject a partially-populated graceful report.
# ---------------------------------------------------------------------------


class DreamAction(BaseModel):
    """One action a dream cycle took / proposed.

    `applied=False` + `candidates>0` means a report-only proposal (P6 default for
    destructive actions). Safe additive actions report what they consolidated.
    """

    tool: str = ''
    container: str | None = None
    summary: str = ''
    candidates: int = 0
    applied: bool = False


class DreamReport(BaseModel):
    """Result of a dreaming cycle (manual trigger or scheduled).

    `excluded_from_rag` is always True — the report lives in the isolated
    governance store, structurally separate from user RAG corpora. `status` is
    'ok' or 'skipped_global_disabled' (global gate off).
    """

    status: str = 'ok'
    started_at: str = ''
    finished_at: str = ''
    container_scope: str = 'all'
    dry_run: bool = True
    excluded_from_rag: bool = True
    actions: list[DreamAction] = Field(default_factory=list)
    notes: str = ''


class DreamContainerStatus(BaseModel):
    """Per-container resolved dreaming config for the status endpoint."""

    container: str
    enabled: bool = True
    cron: str | None = None
    model: str | None = None


class DreamStatusResponse(BaseModel):
    """Body for ``GET /admin/dreaming/status``."""

    global_enabled: bool = True
    scheduler_enabled: bool = False
    scheduler_running: bool = False
    trigger_cron: str = '0 2 * * *'
    batch_model: str = ''
    last_report: DreamReport | None = None
    containers: list[DreamContainerStatus] = Field(default_factory=list)


class DreamTriggerRequest(BaseModel):
    """Body for ``POST /admin/dreaming/trigger`` — manual dreaming kick.

    `dry_run` defaults True (report-only); a real destructive run additionally
    requires config:dreaming:prune_apply true (P6 default false), so this body
    alone can never delete data.
    """

    container: str | None = Field(
        default=None, description='Scope to one container; None = all enabled containers.'
    )
    dry_run: bool = Field(
        default=True, description='True (default) = report-only; never deletes regardless when true.'
    )


# ---------------------------------------------------------------------------
# Governance toolbox (blueprint P6, §A8) — GET /admin/tools,
# POST /admin/tools/{tool}/invoke. The tool registry / execution is implemented
# by the governance toolbox module (Agent B); these models are the read + invoke
# contract the endpoints serialise.
# ---------------------------------------------------------------------------


class ToolInfo(BaseModel):
    """One preset governance tool's static descriptor."""

    name: str
    scope: Literal['global', 'container'] = 'container'
    description: str = ''


class ToolContainerStatus(BaseModel):
    """Resolved + raw enable map for one container.

    `resolved_map` = the container's effective tool switches (container override
    layered over the global map). `raw_map` = the container's own override blob,
    or null when it has none configured (so it fully inherits the global).
    """

    container: str
    resolved_map: dict[str, bool] = Field(default_factory=dict)
    raw_map: dict[str, bool] | None = None


class ToolsListResponse(BaseModel):
    """Body for ``GET /admin/tools``."""

    global_enabled_map: dict[str, bool] = Field(default_factory=dict)
    sandbox_mem_limit: str = '512m'
    approval_ttl_days: int = 30
    new_tool_default_enabled: bool = False
    tools: list[ToolInfo] = Field(default_factory=list)
    containers: list[ToolContainerStatus] = Field(default_factory=list)


class ToolInvokeRequest(BaseModel):
    """Body for ``POST /admin/tools/{tool}/invoke``.

    `dry_run` defaults True（plan 预览，不动数据）。Destructive / LLM tools
    execute for real only on explicit dry_run=false（可逆快照隔离 / 附加式
    索引卡 / 护栏调参），且仍受 enable map 开关约束。
    """

    container: str | None = Field(default=None, description='Target container, or null for global-scope tools.')
    params: dict = Field(default_factory=dict, description='Tool-specific parameters.')
    dry_run: bool = Field(default=True, description='True (default) = plan/preview only, no mutation.')


class ToolInvokeResponse(BaseModel):
    """Body for ``POST /admin/tools/{tool}/invoke``."""

    tool: str
    status: Literal['ok', 'disabled', 'dry_run', 'error', 'deferred', 'applied'] = 'dry_run'
    container: str | None = None
    result: dict = Field(default_factory=dict)
    applied: bool = False
    notes: str = ''


class AgentInvokeRequest(BaseModel):
    """Body for ``POST /admin/agent/{agent_name}/invoke``.

    `dry_run` defaults True（plan 预览，循环全程不落地）。可逆工具仅在
    dry_run=false 且 allow_apply=true 时自动落地；破坏性工具任何情况只进审批队列。
    """

    container: str | None = Field(default=None, description='Target container, or null for a global-scope run.')
    goal: str | None = Field(default=None, description='Natural-language objective for the agent run.')
    params: dict = Field(default_factory=dict, description='Optional run parameters passed through to the runner.')
    dry_run: bool = Field(default=True, description='True (default) = plan/preview only, no mutation.')
    allow_apply: bool = Field(default=False, description='Allow reversible tools to apply (only when dry_run=false).')


class AgentInvokeResponse(BaseModel):
    """Body for ``POST /admin/agent/{agent_name}/invoke``."""

    agent_name: str
    run_id: str
    job_id: int | None = None
    status: Literal['enqueued', 'disabled', 'error'] = 'enqueued'
    container: str | None = None
    dry_run: bool = True
    allow_apply: bool = False
    notes: str = ''


class AgentRunInfo(BaseModel):
    """One row for ``GET /admin/agent/runs``."""

    run_id: str
    agent_name: str = ''
    container: str | None = None
    created_at: int = 0
    status: str = ''
    dry_run: bool = True
    proposals: int = 0
    job_id: int | None = None


class AgentRunsResponse(BaseModel):
    """Body for ``GET /admin/agent/runs``."""

    runs: list[AgentRunInfo] = Field(default_factory=list)


class AgentApprovalInfo(BaseModel):
    """One row for ``GET /admin/agent/approvals``."""

    id: int
    run_id: str = ''
    agent_name: str = ''
    container: str | None = None
    tool: str = ''
    status: str = 'pending'
    created_at: int = 0


class AgentApprovalsResponse(BaseModel):
    """Body for ``GET /admin/agent/approvals``."""

    approvals: list[AgentApprovalInfo] = Field(default_factory=list)
