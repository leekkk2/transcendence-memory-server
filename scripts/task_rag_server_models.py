from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


DEFAULT_CONTAINER = 'imac'


PatternMode = Literal['substring', 'prefix', 'glob']


class SearchReq(BaseModel):
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


class ContainerReq(BaseModel):
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


class ClientIngestReq(BaseModel):
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
    status: Literal['ok']
    service: str
    architecture: str
    build_flavor: Literal['lite', 'full']
    multimodal_capable: bool
    degraded_reasons: list[str] = Field(default_factory=list)
    workspace: str
    containers_root: str
    auth_configured: bool
    embedding_configured: bool
    lancedb_available: bool
    scripts_present: dict[str, bool]
    runtime_ready: dict[str, bool]
    available_containers: list[str]
    warnings: list[str]
    uptime_seconds: int
    modules: dict[str, ModuleStatusResponse] | None = None
    configuration_guide: ConfigurationGuide | None = None


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


class DocumentTextReq(BaseModel):
    container: str = Field(default=DEFAULT_CONTAINER, min_length=1)
    text: str = Field(..., min_length=1)
    description: str | None = None


class QueryReq(BaseModel):
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
