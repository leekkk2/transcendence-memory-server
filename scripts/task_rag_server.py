#!/usr/bin/env python3
"""Canonical FastAPI server for transcendence-memory-server."""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from pathlib import Path

from contextlib import asynccontextmanager

from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

try:
    from task_rag_server_models import (
        AgentOnboardingResponse,
        Citation,
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
        ConfigurationGuide,
        ConnectionTokenResponse,
        ContainerDeleteResponse,
        ContainerListResponse,
        ContainerMetadataPayload,
        ContainerReq,
        DEFAULT_CONTAINER,
        DocumentTextReq,
        HealthResponse,
        IngestMemoryReq,
        BacklogItemResponse,
        BacklogListResponse,
        IndexStatusListResponse,
        IndexStatusResponse,
        JobStatusResponse,
        MemoryDeleteResponse,
        MemoryUpdateResponse,
        ModuleStatusResponse,
        OnboardingPromptResponse,
        PairingAuthResponse,
        QueryReq,
        QueryResponse,
        SearchHit,
        SearchReq,
        SearchResponse,
        StructuredIngestReq,
        UpdateMemoryReq,
        UsageCleanupRequest,
        UsageCleanupResponse,
        UsageContainersResponse,
        UsageEndpointsResponse,
        UsageSummaryResponse,
        UsageTimeseriesResponse,
    )
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_server_models import (
        AgentOnboardingResponse,
        Citation,
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
        ConfigurationGuide,
        ConnectionTokenResponse,
        ContainerDeleteResponse,
        ContainerListResponse,
        ContainerMetadataPayload,
        ContainerReq,
        DEFAULT_CONTAINER,
        DocumentTextReq,
        HealthResponse,
        IngestMemoryReq,
        BacklogItemResponse,
        BacklogListResponse,
        IndexStatusListResponse,
        IndexStatusResponse,
        JobStatusResponse,
        MemoryDeleteResponse,
        MemoryUpdateResponse,
        ModuleStatusResponse,
        OnboardingPromptResponse,
        PairingAuthResponse,
        QueryReq,
        QueryResponse,
        SearchHit,
        SearchReq,
        SearchResponse,
        StructuredIngestReq,
        UpdateMemoryReq,
        UsageCleanupRequest,
        UsageCleanupResponse,
        UsageContainersResponse,
        UsageEndpointsResponse,
        UsageSummaryResponse,
        UsageTimeseriesResponse,
    )

try:
    from rag_engine import get_lightrag
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.rag_engine import get_lightrag

try:
    from raganything_engine import get_raganything
except ModuleNotFoundError:  # pragma: no cover - package import path
    try:
        from scripts.raganything_engine import get_raganything
    except ModuleNotFoundError:
        get_raganything = None  # type: ignore[assignment]

try:
    from arch_detect import detect_architecture, reset_cache as reset_arch_cache
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.arch_detect import detect_architecture, reset_cache as reset_arch_cache

try:
    from server_protection import (
        BG_TRACKER,
        GATE,
        RETRY_LIMITER,
        IngestBusyError,
        read_system_health,
    )
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.server_protection import (
        BG_TRACKER,
        GATE,
        RETRY_LIMITER,
        IngestBusyError,
        read_system_health,
    )

try:
    from job_queue import JobQueue, Job, QueueFullError
    from job_worker import JobWorker, default_command_resolver
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.job_queue import JobQueue, Job, QueueFullError
    from scripts.job_worker import JobWorker, default_command_resolver

try:
    from embed_backlog import BacklogItem, BacklogStore
    from index_state import IndexStateStore, compute_index_state
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.embed_backlog import BacklogItem, BacklogStore
    from scripts.index_state import IndexStateStore, compute_index_state

try:
    from container_metadata import ContainerMetadata
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.container_metadata import ContainerMetadata

try:
    from container_aliases import ContainerAliases, VALID_STATUSES as _ALIAS_VALID_STATUSES
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.container_aliases import ContainerAliases, VALID_STATUSES as _ALIAS_VALID_STATUSES

try:
    import usage_analytics
    from usage_analytics import UsageMiddleware
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import usage_analytics
    from scripts.usage_analytics import UsageMiddleware

# redis_client is import-safe (no eager connect, optional redis dependency) so a
# plain import is fine — it never touches the network and degrades gracefully
# when Redis / the redis package is absent.
try:
    import redis_client
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import redis_client

# config_store is import-safe (no eager connect, cache-only when Redis/DB down)
# and backs the P1 runtime config plane (scalar RAG hot-reload). Reads degrade
# to the profiles.yaml static default, so importing it never changes behavior.
try:
    import config_store
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts import config_store


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))


def _env_int(name: str, default: int) -> int:
    """Read an integer env var with safe fallback.

    Used for back-pressure caps (queue depth, RAG concurrency) so operators
    can tune via TM_* env vars without redeploying the image.
    """
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Async semaphore for the unbounded-cost RAG query path. Document ingestion
# (text/file) no longer runs in-process — it is deferred to the background job
# worker via the persistent queue — so its write/upload semaphores are gone.
import asyncio as _asyncio

_RAG_QUERY_SEM = _asyncio.Semaphore(_env_int('TM_MAX_CONCURRENT_RAG_QUERIES', 2))
SERVER_SCRIPTS = Path(__file__).resolve().parent
WORKSPACE_SCRIPTS = WS / 'scripts'
RAG_API_KEY = os.environ.get('RAG_API_KEY', '')
SERVER_STARTED_AT = time.time()
SKILL_CONFIG_PATH = '~/.transcendence-memory/config.toml'


def script_path(name: str) -> Path:
    workspace_candidate = WORKSPACE_SCRIPTS / name
    server_candidate = SERVER_SCRIPTS / name
    if workspace_candidate.exists():
        return workspace_candidate
    return server_candidate


def container_root(container: str) -> Path:
    path = WS / 'tasks' / 'rag' / 'containers' / container
    path.mkdir(parents=True, exist_ok=True)
    return path


def container_metadata_uri() -> str:
    """容器命名规范元数据表（container_metadata）所在 LanceDB 目录。

    与每容器自有的 ``containers/<c>/lancedb/`` 物理隔离，独占一个 connection。
    懒创建：首次 upsert 时由 ContainerMetadata 内部 create_table。
    """
    base = WS / 'tasks' / 'rag' / 'meta' / 'lancedb'
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


_container_metadata_store: ContainerMetadata | None = None
_container_aliases_store: ContainerAliases | None = None
# 进程内 alias 缓存：alias name → row dict（含 None 哨兵以缓存"未命中"）。
# SIGHUP / 重启即清空；admin upsert/delete alias 主动失效对应 key。
_alias_cache: dict[str, Optional[dict]] = {}
_alias_cache_lock = threading.Lock()


def get_container_metadata_store() -> ContainerMetadata:
    """单例 ContainerMetadata；首次访问时连库。"""
    global _container_metadata_store
    if _container_metadata_store is None:
        _container_metadata_store = ContainerMetadata(container_metadata_uri())
    return _container_metadata_store


def get_container_aliases_store() -> ContainerAliases:
    """单例 ContainerAliases；首次访问时连库。共用 container_metadata 的 LanceDB uri。"""
    global _container_aliases_store
    if _container_aliases_store is None:
        _container_aliases_store = ContainerAliases(container_metadata_uri())
    return _container_aliases_store


def _alias_cache_invalidate(alias: str | None = None) -> None:
    """alias 写入 / 删除后失效缓存。alias=None 时清整张表。"""
    with _alias_cache_lock:
        if alias is None:
            _alias_cache.clear()
        else:
            _alias_cache.pop(alias, None)


def resolve_container_or_raise(name: str) -> tuple[str, Optional[dict]]:
    """把客户端传入的 container name 解析为 canonical。

    返回 ``(canonical_name, alias_row_or_None)``：
      - 未命中 alias 表 → ``(name, None)``。caller 沿用原有"已有即用、新名自动创建"
        逻辑，行为完全向后兼容。
      - status='active'     → ``(canonical, row)``，无感透传。
      - status='deprecated' → 同 active，但记 warning 日志（提示客户端升级）。
      - status='removed'    → 抛 ``HTTPException(410)``，防客户端"幽灵重建"已删容器。

    使用进程内缓存（含 None 哨兵）减少 LanceDB 重复读；alias 写入 / 删除路径
    主动 invalidate；进程重启 / SIGHUP 自动清空。
    """
    if not name:
        return (name, None)
    # 缓存读
    with _alias_cache_lock:
        cached = _alias_cache.get(name, _MISSING)
    if cached is _MISSING:
        try:
            row = get_container_aliases_store().resolve(name)
        except Exception:  # pragma: no cover - 表故障不应阻塞主路径
            logger.exception("alias table read failed for %r; treating as canonical", name)
            row = None
        with _alias_cache_lock:
            _alias_cache[name] = row
    else:
        row = cached

    if row is None:
        return (name, None)

    status = row.get("status", "active")
    if status == "removed":
        raise HTTPException(
            status_code=410,
            detail={
                "error": "container_removed",
                "removed_alias": name,
                "removed_at": row.get("updated_at"),
                "reason": row.get("reason"),
                "suggestion": (
                    row.get("notes")
                    or "Use 'legacy-default' (default) or contact ops for a current canonical container."
                ),
            },
        )
    if status == "deprecated":
        logger.warning(
            "deprecated alias %r → %r (suggested upgrade)",
            name,
            row.get("canonical"),
        )
    return (row["canonical"], row)


# Sentinel 用于区分"未缓存"与"缓存了 None"。
_MISSING: Any = object()


def memory_objects_path(container: str) -> Path:
    return container_root(container) / 'memory_objects.jsonl'


def build_connection_onboarding(endpoint: str, container: str, api_key: str) -> tuple[PairingAuthResponse, AgentOnboardingResponse]:
    pairing_auth = PairingAuthResponse(
        mode='api_key',
        endpoint=endpoint,
        api_key=api_key,
        container=container,
        accepted_headers=['X-API-KEY', 'Authorization: Bearer <api_key>'],
        token_transport='base64-json(endpoint, api_key, container)',
        config_path=SKILL_CONFIG_PATH,
    )
    onboarding = AgentOnboardingResponse(
        collect_from_user=[
            OnboardingPromptResponse(
                id='who_is_pairing_for',
                title='确认使用主体',
                prompt='这次要为哪个 Agent、设备或项目配对？如果你希望隔离记忆，请告诉我你想使用的名称。',
                reason='帮助 AI 按 Agent / 设备 / 项目拆分命名空间，避免不同上下文写入同一 container。',
            ),
            OnboardingPromptResponse(
                id='confirm_container',
                title='确认 container',
                prompt=f'我准备把你连接到 container "{container}"。如果你想改成别的命名空间，请现在告诉我。',
                reason='让用户在导入前确认最终写入的 container。',
            ),
            OnboardingPromptResponse(
                id='choose_pairing_mode',
                title='选择配对方式',
                prompt='你希望我直接导入 connection token，还是把 endpoint / api_key / container 展示给你手动配置？',
                reason='有些用户偏好一键导入，有些用户需要显式查看和保存鉴权材料。',
            ),
            OnboardingPromptResponse(
                id='confirm_local_write',
                title='确认本地落盘',
                prompt=f'继续后，技能端通常会把 endpoint、container 和 API key 写入 {SKILL_CONFIG_PATH}。是否继续？',
                reason='让用户明确知道哪些配对信息会被写入本地配置。',
            ),
        ],
        tell_user=[
            f'当前 skill 端会连接到 endpoint "{endpoint}"，默认 container 为 "{container}"。',
            '当前 skill 端鉴权模式为 api_key，服务端同时接受 X-API-KEY 与 Authorization: Bearer <api_key> 两种头部。',
            '这次返回的 connection token 本质上是一个 base64 JSON，里面包含 endpoint、api_key、container 三项配对材料。',
            '如果用户选择手动模式，请明确展示 pairing_auth 中的 endpoint、api_key、container，而不是只告诉用户 token 已生成。',
            f'导入完成后，技能端通常会把这些信息写入 {SKILL_CONFIG_PATH}。',
        ],
        recommended_commands=['/tm connect <token-from-this-response>', '/tm connect --manual'],
    )
    return pairing_auth, onboarding


# ---------------------------------------------------------------------------
# Admin dashboard session + login rate limit — module-level singletons,
# lazily constructed under the workspace just like JOB_QUEUE et al. We
# centralise the path here so tests can drive both stores through their
# WORKSPACE override without poking module internals.
# ---------------------------------------------------------------------------

_UI_SESSION_STORE: 'auth_session.SessionStore | None' = None
_UI_LOGIN_LIMIT: 'auth_session.LoginRateLimit | None' = None


def _ui_db_path() -> Path:
    """Single SQLite file co-located with the queue DB.

    Lives under tasks/rag/ alongside queue.db so the entire mutable runtime
    state stays in one subtree — easier to back up and to wipe in tests.
    """
    return WS / 'tasks' / 'rag' / 'ui_sessions.db'


def get_ui_session_store():
    """Return the process-wide ``SessionStore`` singleton, building it on demand."""
    global _UI_SESSION_STORE
    if _UI_SESSION_STORE is None:
        import auth_session  # local import keeps cold-start cheap
        _UI_SESSION_STORE = auth_session.SessionStore(
            _ui_db_path(), ttl_sec=auth_session.env_ttl()
        )
    return _UI_SESSION_STORE


def get_ui_login_limit():
    """Return the process-wide ``LoginRateLimit`` singleton."""
    global _UI_LOGIN_LIMIT
    if _UI_LOGIN_LIMIT is None:
        import auth_session
        _UI_LOGIN_LIMIT = auth_session.LoginRateLimit(
            _ui_db_path(),
            lockout_count=auth_session.env_lockout_count(),
            window_sec=auth_session.env_lockout_window(),
        )
    return _UI_LOGIN_LIMIT


def _reset_ui_singletons() -> None:
    """Test helper — wipes the lazily built stores so a per-test WORKSPACE
    override is honoured the next time ``get_ui_session_store`` is called."""
    global _UI_SESSION_STORE, _UI_LOGIN_LIMIT
    _UI_SESSION_STORE = None
    _UI_LOGIN_LIMIT = None


def verify_auth(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    """Gate every protected endpoint. Cookie session is preferred — header
    auth is the legacy CLI / programmatic path and stays first-class so older
    clients keep working without rebuilding around the dashboard.

    Resolution order:
      1. ``tm_sid`` cookie → resolve via SessionStore (browser path)
      2. ``X-API-KEY`` / ``Authorization: Bearer`` header (CLI / SDK path)
    Either match succeeds; otherwise 401.
    """
    if not RAG_API_KEY:
        raise HTTPException(status_code=500, detail='RAG_API_KEY not set')

    # 1) cookie session — preferred for the SPA so the api key never has to
    #    live in JS memory after login. Validity check is O(1) sqlite lookup.
    token = request.cookies.get('tm_sid') if request is not None else None
    if token:
        session = get_ui_session_store().validate(token)
        if session is not None and session.api_key_hash:
            from auth_session import hash_api_key
            if session.api_key_hash == hash_api_key(RAG_API_KEY):
                return

    # 2) header — what the CLI / curl / SDK clients have always used.
    key = None
    if authorization and authorization.lower().startswith('bearer '):
        key = authorization.split(' ', 1)[1]
    elif x_api_key:
        key = x_api_key
    if key != RAG_API_KEY:
        raise HTTPException(status_code=401, detail='unauthorized')


logger = logging.getLogger('transcendence-memory-server')


def _startup_banner() -> None:
    arch = detect_architecture()
    lines = [
        '',
        '=' * 56,
        '  Transcendence Memory Server',
        f'  Build Flavor: {arch.build_flavor}',
        f'  Architecture: {arch.name}',
        '-' * 56,
    ]
    status_icons = {True: '[OK]', False: '[--]'}
    for mod_name, mod in arch.modules.items():
        icon = status_icons[mod.enabled]
        detail = ''
        if mod.missing_keys:
            detail = f' (missing: {", ".join(mod.missing_keys)})'
        elif not mod.package_available:
            detail = ' (package not installed)'
        lines.append(f'  {icon} {mod_name:<15} {"ready" if mod.ready else "disabled"}{detail}')
    if arch.missing_keys:
        lines.append('-' * 56)
        lines.append('  To unlock full rag-everything:')
        for key in arch.missing_keys:
            lines.append(f'    - Set {key} in .env')
    if arch.degraded_reasons:
        lines.append('-' * 56)
        for reason in arch.degraded_reasons:
            lines.append(f'  [WARN] {reason}')
    lines.append('=' * 56)
    lines.append('')
    for line in lines:
        logger.info(line)


# 队列在导入时不创建，等 lifespan 启动时按 WORKSPACE 实例化。
# WORKER 持有对全局队列的引用，启停由 lifespan 控制。
JOB_QUEUE: JobQueue | None = None
JOB_WORKER: JobWorker | None = None
# 测试可设为 True 来禁用 worker 自动启动；其他场景应保持 False。
DISABLE_WORKER = os.environ.get('TM_DISABLE_WORKER', '0') in ('1', 'true', 'True')


def _queue_db_path() -> Path:
    return WS / 'tasks' / 'rag' / 'queue.db'


def get_job_queue() -> JobQueue:
    global JOB_QUEUE
    if JOB_QUEUE is None:
        JOB_QUEUE = JobQueue(_queue_db_path())
    return JOB_QUEUE


# embedding backlog 与 container_index_state 都复用 JobQueue 的 queue.db
# （单一 WAL / 崩溃恢复 / purge 域）。惰性单例，按 WORKSPACE 实例化。
BACKLOG_STORE: BacklogStore | None = None
INDEX_STATE_STORE: IndexStateStore | None = None


def get_backlog_store() -> BacklogStore:
    global BACKLOG_STORE
    if BACKLOG_STORE is None:
        BACKLOG_STORE = BacklogStore(_queue_db_path())
    return BACKLOG_STORE


def get_index_state_store() -> IndexStateStore:
    global INDEX_STATE_STORE
    if INDEX_STATE_STORE is None:
        INDEX_STATE_STORE = IndexStateStore(_queue_db_path())
    return INDEX_STATE_STORE


# /embed 与 /ingest-memory/objects 响应里附带的说明：embedding 失败不丢对象。
_BACKLOG_NOTE = (
    'Objects whose embedding fails upstream (e.g. quota exhaustion or timeout) '
    'are recorded in a retry backlog and retried silently in the background — '
    'they are not lost. See GET /containers/{name}/index-status for state.'
)


def _verify_embedding_dim_consistency() -> None:
    """Fail-fast if LanceDB chunks tables disagree with EMBEDDING_DIM.

    Background: a 2026-05-29 incident silently shipped EMBEDDING_DIM=3072 against
    1024-dim LanceDB tables, causing all /search queries to throw
    `RuntimeError: query dim doesn't match column vector dim` for ~14h. Healthcheck
    stayed green because /health never issues a vector query. This guard refuses
    to enter the serving loop until the runtime dim matches stored vectors.

    Override (use sparingly, e.g. a planned reindex): TM_ALLOW_DIM_DRIFT=1.
    """
    if os.environ.get('TM_ALLOW_DIM_DRIFT', '0') in ('1', 'true', 'True'):
        print('[startup-check] TM_ALLOW_DIM_DRIFT=1 set — skipping dim consistency check', flush=True)
        return
    expected_dim_raw = os.environ.get('EMBEDDING_DIM')
    if not expected_dim_raw:
        # No explicit env; defer to profiles.yaml / legacy fallback inside the runtime.
        # The check is best-effort here — we only act when EMBEDDING_DIM is set.
        return
    try:
        expected_dim = int(expected_dim_raw)
    except ValueError:
        print(f'[startup-check] EMBEDDING_DIM={expected_dim_raw!r} not an int — skipping', flush=True)
        return
    try:
        import lancedb  # type: ignore
    except ImportError:
        print('[startup-check] lancedb not importable — skipping dim check', flush=True)
        return
    containers_root = WS / 'tasks' / 'rag' / 'containers'
    if not containers_root.is_dir():
        return  # fresh install, nothing to verify
    mismatches: list[tuple[str, int]] = []
    checked = 0
    for cdir in sorted(containers_root.iterdir()):
        lancedb_dir = cdir / 'lancedb'
        if not (lancedb_dir / 'chunks.lance').is_dir():
            continue
        try:
            db = lancedb.connect(str(lancedb_dir))
            tbl = db.open_table('chunks')
            vec_field = next((f for f in tbl.schema if f.name == 'vector'), None)
            if vec_field is None:
                continue
            # pyarrow fixed_size_list -> .type.list_size
            actual_dim = getattr(vec_field.type, 'list_size', None)
            if actual_dim is None:
                continue
            checked += 1
            if actual_dim != expected_dim:
                mismatches.append((cdir.name, actual_dim))
        except Exception as exc:  # noqa: BLE001 — startup probe must not crash on a bad table
            print(f'[startup-check] container={cdir.name} schema probe failed: {exc}', flush=True)
    if mismatches:
        print('=' * 56, flush=True)
        print(f'[startup-check] FATAL: EMBEDDING_DIM={expected_dim} disagrees with LanceDB schemas:', flush=True)
        for name, dim in mismatches:
            print(f'  - container={name}: stored dim={dim}', flush=True)
        print('  Cause: .env drift vs profiles.yaml, or accidental model swap.', flush=True)
        print('  Action: align EMBEDDING_DIM (and EMBEDDING_MODEL) with stored dim,', flush=True)
        print('          OR rebuild affected containers with the new model,', flush=True)
        print('          OR set TM_ALLOW_DIM_DRIFT=1 if you are mid-reindex.', flush=True)
        print('  Refusing to start to prevent silent /search RuntimeError storm.', flush=True)
        print('=' * 56, flush=True)
        sys.exit(1)
    print(f'[startup-check] embedding dim consistency OK ({checked} container(s) @ dim={expected_dim})', flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global JOB_WORKER
    _startup_banner()
    _verify_embedding_dim_consistency()
    # Redis governance foundation (blueprint P0). Non-fatal: a down/disabled
    # Redis only logs `degraded` and the main RAG path is unaffected. We do one
    # best-effort ping so the log clearly states startup posture.
    if await redis_client.init_pool():
        if await redis_client.is_available():
            logger.info('[redis] connected — governance state available')
        else:
            logger.warning('[redis] configured but ping failed at startup — '
                           'running degraded (governance falls back to defaults)')
    else:
        logger.info('[redis] disabled — governance falls back to defaults')
    # Config center (blueprint P1). Non-fatal: load persisted scalar overrides
    # into the process cache, then start the live `config_updated` subscriber.
    # A down Redis/DB only means no hot-reload — every config read falls back to
    # the profiles.yaml static value, so the main RAG path is unaffected.
    try:
        await config_store.load_all()
        await config_store.start_config_subscriber()
    except Exception as exc:  # noqa: BLE001 - config plane must never block boot
        logger.warning('[config] init failed, running on static defaults: %s', exc)
    queue = get_job_queue()
    if not DISABLE_WORKER:
        scripts_dir = SERVER_SCRIPTS  # task_rag_lancedb_ingest.py 等都在这里
        JOB_WORKER = JobWorker(
            queue=queue,
            command_resolver=default_command_resolver(scripts_dir),
        )
        JOB_WORKER.start()
    try:
        yield
    finally:
        if JOB_WORKER is not None:
            JOB_WORKER.stop(join_timeout=10.0)
        await config_store.stop_config_subscriber()
        await redis_client.close_pool()


app = FastAPI(lifespan=lifespan)


_MAX_UPLOAD_BYTES_ENV = int(os.environ.get('MAX_UPLOAD_BYTES', str(200 * 1024 * 1024)))


@app.middleware('http')
async def _enforce_upload_limit(request, call_next):
    """对 multipart 上传预检 Content-Length，避免 Starlette 先把整份 body 落盘。"""
    if request.url.path == '/documents/file' and request.method == 'POST':
        cl = request.headers.get('content-length')
        if cl is not None:
            try:
                size = int(cl)
            except ValueError:
                size = -1
            if size > _MAX_UPLOAD_BYTES_ENV:
                from fastapi.responses import JSONResponse, StreamingResponse
                return JSONResponse(
                    status_code=413,
                    content={'detail': f'file exceeds max upload size {_MAX_UPLOAD_BYTES_ENV} bytes'},
                )
    return await call_next(request)


# Usage analytics middleware (v0.17). Registered once, lazily creates the
# SQLite tables on first request. Gated by TM_USAGE_ANALYTICS so a
# deployment can turn it off without rebuilding the image.
if os.environ.get('TM_USAGE_ANALYTICS', '1') in ('1', 'true', 'True'):
    app.add_middleware(
        UsageMiddleware,
        db_path=str(_queue_db_path()),
        enabled=True,
        log_ua=os.environ.get('TM_USAGE_LOG_UA', '1') in ('1', 'true', 'True'),
        max_body_inspect=int(os.environ.get('TM_USAGE_MAX_BODY_INSPECT', str(1_048_576))),
    )


def child_env(
    embedding_override: str | None = None,
    container: str = '',
    embed_mode: str | None = None,
) -> dict[str, str]:
    """子进程 env 构造。

    embed_mode：P0 asymmetric retrieval —— `/search` 子进程注入
    `TM_EMBED_MODE=query`，让 task_rag_runtime.embed_text 对 gemini-embedding-2
    查询走 query 前缀；摄取子进程不注入 → embed_text 默认 document。

    embedding_override：来自 request 的 `embedding_model`。worker (task_rag_runtime)
    resolve 时若读到 `TM_EMBEDDING_PROFILE_OVERRIDE`，优先用它取代 route 默认 profile。
    Phase 1 的最小注入面：env var，不动 worker CLI 签名。

    container：当前请求作用的 container 名。注入 `CONTAINER` env 让 subprocess
    (task_rag_search.py 等) 的 task_rag_runtime._resolve_chain_for_worker 走
    "CONTAINER env → registry.resolve(container)" 路径，而不是退化到 default route。
    v0.10.1 修了 JobWorker 路径同名 bug；v0.10.2 这里补全同步 subprocess 路径。
    """
    env = os.environ.copy()
    if env.get('EMBEDDING_BASE_URL') and not env.get('EMBEDDINGS_BASE_URL'):
        env['EMBEDDINGS_BASE_URL'] = env['EMBEDDING_BASE_URL']
    # Force UTF-8 for subprocess JSON I/O so Windows pipes can safely carry non-ASCII text.
    env.setdefault('PYTHONUTF8', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    if embedding_override:
        env['TM_EMBEDDING_PROFILE_OVERRIDE'] = embedding_override
    if container:
        env['CONTAINER'] = container
    if embed_mode:
        env['TM_EMBED_MODE'] = embed_mode
    return env


def run(
    cmd: list[str],
    timeout_s: int,
    *,
    embedding_override: str | None = None,
    container: str = '',
    embed_mode: str | None = None,
) -> CommandResponse:
    script = Path(cmd[0])
    if not script.exists():
        return CommandResponse(command=cmd, code=127, stderr=f'script not found: {script}')
    real_cmd = [sys.executable, *cmd] if cmd[0].endswith('.py') else cmd
    try:
        completed = subprocess.run(
            real_cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout_s,
            env=child_env(embedding_override, container=container, embed_mode=embed_mode),
        )
        return CommandResponse(command=real_cmd, code=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
    except subprocess.TimeoutExpired as exc:
        return CommandResponse(
            command=real_cmd,
            code=124,
            stdout=exc.stdout or '',
            stderr=f'{exc.stderr or ""}\ntimeout after {timeout_s}s'.strip(),
        )
    except Exception as exc:  # pragma: no cover - subprocess edge varies
        return CommandResponse(command=real_cmd, code=1, stderr=f'command failed: {exc}')


def run_or_start(
    cmd: list[str],
    timeout_s: int,
    background: bool | None,
    wait: bool,
    *,
    container: str = '',
    embedding_override: str | None = None,
) -> CommandResponse:
    """**DEPRECATED 自 v0.10.2** — 全仓 grep 无内部调用方（合并入 _enqueue_or_run 后留下）。
    保留签名以防外部第三方代码引用；计划 v0.11.0 删除。新代码不要使用此函数。"""
    if not Path(cmd[0]).exists():
        return CommandResponse(command=cmd, code=127, stderr=f'script not found: {cmd[0]}')
    real_cmd = [sys.executable, *cmd] if cmd[0].endswith('.py') else cmd
    run_in_background = background if background is not None else not wait
    if run_in_background:
        # 后台路径：在派生子进程前问 BG_TRACKER 是否还有容量，
        # 防止在压力下被外部循环调用而无限堆叠。
        ok, reason = BG_TRACKER.has_capacity()
        if not ok:
            return CommandResponse(
                command=real_cmd,
                code=429,
                background=False,
                wait=False,
                status='rejected',
                note=f'background job pool full: {reason}',
                stderr=reason,
            )
        process = subprocess.Popen(
            real_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=child_env(embedding_override, container=container),
        )
        BG_TRACKER.register(process.pid, container=container or 'unknown', label=Path(cmd[0]).name)
        return CommandResponse(
            command=real_cmd,
            code=0,
            background=True,
            wait=False,
            pid=process.pid,
            status='started',
            note='Background ingest started.',
        )
    result = run(cmd, timeout_s=timeout_s, embedding_override=embedding_override, container=container)
    result.background = False
    result.wait = True
    return result


def _admit_or_503(container: str, op: str) -> None:
    """重型端点统一的准入检查。

    分三步：1) 系统健康预检；2) 后台池容量；3) 全局/容器并发锁由调用方用 GATE.acquire 包裹。
    任何一步失败都抛 503，附带 Retry-After 让客户端指数退避。
    """
    snap = read_system_health()
    ok, reason = GATE.check_admit(snap)
    if not ok:
        logger.warning('admit_denied op=%s container=%s reason=%s', op, container, reason)
        raise HTTPException(
            status_code=503,
            detail={
                'error': 'system_under_pressure',
                'op': op,
                'container': container,
                'reason': reason,
                'system': snap.as_dict(),
            },
            headers={'Retry-After': '30'},
        )
    ok, reason = BG_TRACKER.has_capacity()
    if not ok:
        logger.warning('admit_denied op=%s container=%s reason=%s', op, container, reason)
        raise HTTPException(
            status_code=503,
            detail={'error': 'background_pool_full', 'op': op, 'reason': reason},
            headers={'Retry-After': '15'},
        )


def _gate_status_labels(snap, config) -> dict[str, str]:
    """每个压力维度返回 'ok' / 'pressure'，不带数值。

    用途：公开 /health 想给客户端一个"是否需要退避"信号，但又不能像鉴权端点
    那样直接交出阈值 + 实测值（攻击者会用来构造边缘 DoS）。
    """
    out: dict[str, str] = {}
    eff_mem = snap.effective_mem_available_mb
    if eff_mem is not None:
        out['memory'] = 'pressure' if eff_mem < config.min_available_mem_mb else 'ok'
    if snap.load_per_cpu is not None:
        out['load'] = 'pressure' if snap.load_per_cpu > config.max_load_per_cpu else 'ok'
    if snap.swap_used_pct is not None:
        out['swap'] = 'pressure' if snap.swap_used_pct > config.max_swap_used_pct else 'ok'
    return out


def _redact_admit_reason(reason: str) -> str:
    """把 'memory pressure: available=747MB < threshold 800MB' 缩成 'memory pressure'。"""
    if 'memory pressure' in reason:
        return 'memory pressure'
    if 'system load high' in reason:
        return 'load pressure'
    if 'swap pressure' in reason:
        return 'swap pressure'
    return 'system under pressure'


async def _collect_health_state(container: str | None) -> dict:
    """收集 health 状态的唯一来源。公开 /health 取最小子集，鉴权 /admin/system-health 拿全集。

    返回 dict 中所有 key 都填充；调用方决定哪些字段返给用户。
    """
    containers = WS / 'tasks' / 'rag' / 'containers'
    scripts_present = {
        'search': script_path('task_rag_search.py').exists(),
        'lancedb_ingest': script_path('task_rag_lancedb_ingest.py').exists(),
        'structured_ingest': script_path('task_rag_structured_ingest.py').exists(),
    }
    embedding_configured = bool(os.environ.get('EMBEDDING_API_KEY'))
    lancedb_available = importlib.util.find_spec('lancedb') is not None

    # 双轨 warnings：公开版脱敏（不带数值/路径/key 名），admin 版保留原文
    public_warnings: list[str] = []
    sensitive_warnings: list[str] = []
    if not RAG_API_KEY:
        public_warnings.append('auth not configured')
        sensitive_warnings.append('RAG_API_KEY is not configured.')
    if not embedding_configured:
        public_warnings.append('embedding not configured')
        sensitive_warnings.append('EMBEDDING_API_KEY is not configured.')
    if not lancedb_available:
        public_warnings.append('lancedb runtime unavailable')
        sensitive_warnings.append('lancedb runtime is unavailable.')
    if not containers.exists():
        sensitive_warnings.append('containers root does not exist yet; it will be created on first ingest.')

    arch = detect_architecture()
    # degraded_reasons 是业务侧降级原因（如"lite build VLM 不可用"），不含数值/路径，公开安全
    public_warnings.extend(arch.degraded_reasons)
    sensitive_warnings.extend(arch.degraded_reasons)

    modules_resp = {
        name: ModuleStatusResponse(
            enabled=mod.enabled,
            ready=mod.ready,
            package_available=mod.package_available,
            required_keys=mod.required_keys,
            missing_keys=mod.missing_keys,
        )
        for name, mod in arch.modules.items()
    }
    config_guide = ConfigurationGuide(
        configured=arch.configured_keys,
        missing=arch.missing_keys,
        optional=arch.optional_keys,
    )

    documents_text_ready = arch.modules['lightrag'].ready
    if container and documents_text_ready:
        validate_container_name(container)
        try:
            await get_lightrag(container)
        except Exception as exc:
            documents_text_ready = False
            public_warnings.append('LightRAG probe failed')
            sensitive_warnings.append(f'LightRAG probe failed for container={container}: {exc}')

    sys_snap = read_system_health()
    admit_ok, admit_reason = GATE.check_admit(sys_snap)
    if not admit_ok:
        public_warnings.append(_redact_admit_reason(admit_reason))
        sensitive_warnings.append(f'system pressure: {admit_reason}')

    try:
        queue_stats = get_job_queue().stats()
    except Exception as exc:  # pragma: no cover - defensive
        queue_stats = {'error': str(exc)}
        public_warnings.append('job queue inaccessible')
        sensitive_warnings.append(f'job queue inaccessible: {exc}')
    worker_running = bool(JOB_WORKER and JOB_WORKER.is_running)
    if not worker_running and not DISABLE_WORKER:
        public_warnings.append('background ingest worker not running')
        sensitive_warnings.append('background ingest worker is not running')

    # embedding backlog 概览 —— 仅 sensitive 轨：容器名不进公开视图（避免给
    # 匿名访问者积累指纹）；公开 /health 只能从 system_status 看压力标签。
    try:
        bstore = get_backlog_store()
        backlog_n = quota_n = dead_n = 0
        for c in bstore.all_active_containers():
            csum = bstore.summary(c)
            if csum['active'] > 0:
                backlog_n += 1
                if csum['last_error_class'] == 'quota':
                    quota_n += 1
            if csum['dead'] > 0:
                dead_n += 1
        backlog_parts: list[str] = []
        if backlog_n:
            backlog_parts.append(f'{backlog_n} container(s) with embedding backlog')
        if quota_n:
            backlog_parts.append(f'{quota_n} quota-blocked')
        if dead_n:
            backlog_parts.append(f'{dead_n} with permanent embedding failures')
        if backlog_parts:
            sensitive_warnings.append('; '.join(backlog_parts) + '.')
    except Exception as exc:  # pragma: no cover - defensive
        sensitive_warnings.append(f'embedding backlog status unavailable: {exc}')

    runtime_ready = {
        'search': scripts_present['search'] and embedding_configured and lancedb_available,
        'embed': scripts_present['lancedb_ingest'] and embedding_configured and lancedb_available,
        'ingest_memory': scripts_present['lancedb_ingest'] and embedding_configured and lancedb_available,
        'ingest_objects': True,
        'ingest_structured': scripts_present['structured_ingest'] and embedding_configured and lancedb_available,
        'query': documents_text_ready,
        'documents_text': documents_text_ready,
    }
    avail_containers = sorted(p.name for p in containers.iterdir() if p.is_dir()) if containers.exists() else []

    # ── profile summary（仅 admin 用）。失败不阻塞 health，记 sensitive warning。
    # 公开 /health 不取这个字段，避免泄漏 profile 名 → 加固 user 已收口的安全契约。
    profiles_summary: dict | None = None
    try:
        try:
            from embedding_registry import get_registry as _get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry as _get_registry
        ps = _get_registry().profiles
        profiles_summary = {
            'embeddings_count': len(ps.embeddings),
            'rerankers_count': len(ps.rerankers),
            'default_route_embedding': ps.default_route.embedding if ps.default_route else None,
        }
    except Exception as exc:  # pragma: no cover - 配置错误会被 admin 看见
        sensitive_warnings.append(f'profiles registry unavailable: {exc}')

    return {
        # ──── 公开（HealthResponse 用）────
        'status': 'ok',
        'service': 'transcendence-memory-server',
        'architecture': arch.name,
        'build_flavor': arch.build_flavor,
        'multimodal_capable': arch.multimodal_capable,
        'degraded_reasons': arch.degraded_reasons,
        'runtime_ready': runtime_ready,
        'accepting_ingest': admit_ok,
        'worker_running': worker_running,
        'uptime_seconds': max(0, int(time.time() - SERVER_STARTED_AT)),
        'system_status': _gate_status_labels(sys_snap, GATE.config),
        'public_warnings': public_warnings,
        # ──── 仅鉴权端点（admin_system_health 用）────
        'workspace': str(WS),
        'containers_root': str(containers),
        'auth_configured': bool(RAG_API_KEY),
        'embedding_configured': embedding_configured,
        'lancedb_available': lancedb_available,
        'scripts_present': scripts_present,
        'available_containers': avail_containers,
        'modules': modules_resp,
        'configuration_guide': config_guide,
        'system': sys_snap.as_dict(),
        'thresholds': GATE.config.as_dict(),
        'admit_reason': admit_reason,
        'background_jobs_active': BG_TRACKER.count_active(),
        'queue_stats': queue_stats,
        'profiles': profiles_summary,
        'sensitive_warnings': sensitive_warnings,
    }


@app.get('/health', response_model=HealthResponse)
async def health(container: str | None = None) -> HealthResponse:
    state = await _collect_health_state(container)
    return HealthResponse(
        status=state['status'],
        service=state['service'],
        architecture=state['architecture'],
        build_flavor=state['build_flavor'],
        multimodal_capable=state['multimodal_capable'],
        degraded_reasons=state['degraded_reasons'],
        runtime_ready=state['runtime_ready'],
        accepting_ingest=state['accepting_ingest'],
        worker_running=state['worker_running'],
        uptime_seconds=state['uptime_seconds'],
        system_status=state['system_status'],
        warnings=state['public_warnings'],
    )


def _get_union_search_default() -> bool:
    """从 ProfileSet 拿 union_search_default 顶层开关。

    v0.11.0：单 container 查询时根据该开关决定是否自动 union sibling _openai 镜像。
    任何加载异常都视为 false，保证旧部署 / 缺 YAML 场景不被破坏。
    """
    try:
        try:
            from embedding_registry import get_registry as _get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry as _get_registry
        return bool(_get_registry().profiles.union_search_default)
    except Exception:
        return False


def _get_union_per_container_timeout() -> float:
    """从 ProfileSet 拿 union 多容器子查询 per-container timeout（秒）。

    Phase 1：旧 12.0 误杀冷启动主容器致整条降级失败，默认放宽到 30.0。任何加载
    异常回退 `_DEFAULT_PER_CONTAINER_TIMEOUT_S`（与 _get_union_search_default 同风格）。
    """
    try:
        try:
            from embedding_registry import get_registry as _get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry as _get_registry
        return float(_get_registry().profiles.union_per_container_timeout_s)
    except Exception:
        return _DEFAULT_PER_CONTAINER_TIMEOUT_S


def _get_score_threshold_default() -> float | None:
    """从 ProfileSet 拿 score-gate 阈值（L2 距离上界，None=关闭）。

    Phase 1：默认关闭（opt-in）。任何加载异常回退 None（关闭），不破坏旧路径。

    P1 配置中心：profiles.yaml 的静态值作为 default，仅当 Redis/DB 有热重载覆盖
    （Dashboard 改过）时才用覆盖值。无覆盖时 get_cached 原样返回 default →
    与 P1 前逐字节一致（similarity_threshold 仍 None）。config_store 全程降级安全。
    """
    try:
        try:
            from embedding_registry import get_registry as _get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry as _get_registry
        static_default = _get_registry().profiles.similarity_threshold
    except Exception:
        static_default = None
    return config_store.get_cached('config:rag:similarity_threshold', static_default)


def _get_citation_enabled() -> bool:
    """是否在 /search 响应里投影 citations 数组。异常回退 True（默认开）。

    P1 配置中心：同 _get_score_threshold_default —— profiles 静态值作 default，
    有热重载覆盖才用覆盖；无覆盖逐字节不变（仍默认 True）。
    """
    try:
        try:
            from embedding_registry import get_registry as _get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry as _get_registry
        static_default = bool(_get_registry().profiles.citation_enabled)
    except Exception:
        static_default = True
    return config_store.get_cached('config:rag:citation_enabled', static_default)


def _container_has_chunks_table(name: str) -> bool:
    """探测容器是否已 embed（存在可打开的 'chunks' LanceDB 表）。只连不 embedding。

    Phase 1 项2：未初始化的 sibling 镜像在 union 解析阶段就被软跳过，避免 /search
    返回 not_initialized 噪音拖垮整条降级。任何异常（目录缺失 / connect 失败 / 表不
    存在）吞为 False —— 不可达即视为未就绪，宁可少 union 一路也不报错。

    用 ``open_table`` 直探而非解析 ``list_tables()``：本仓 lancedb 版本的
    ``list_tables()`` 返回分页结构（``[('tables', [...]), ('page_token', None)]``），
    不便可靠解析；``open_table`` 成功即证表就绪，是最稳的存在性判据。
    """
    try:
        import lancedb
        try:
            from task_rag_runtime import lancedb_dir
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.task_rag_runtime import lancedb_dir
        db = lancedb.connect(str(lancedb_dir(name)))
        db.open_table('chunks')
        return True
    except Exception:
        return False


_OPENAI_MIRROR_SUFFIX = '_openai'


def _resolve_search_targets(req: SearchReq) -> tuple[list[str], bool]:
    """根据 SearchReq 解析最终要查询的容器列表。

    优先级：containers > container_pattern > container（含 v0.11.0 union 扩展）。
    返回 (targets, union_applied)：union_applied=True 表示触发了 sibling _openai 自动追加。

    alias resolution：每个入参容器都经 ``resolve_container_or_raise`` 透传到
    canonical；status='removed' 直接 410（防客户端"幽灵重建"）。pattern 同时匹配
    canonical 容器名 + 已注册 alias 的 alias 字段。
    """
    if req.containers:
        # 显式 containers list → 用户已掌控全部目标，不再触发 union 扩展
        for name in req.containers:
            validate_container_name(name)
        seen: set[str] = set()
        ordered: list[str] = []
        for name in req.containers:
            canonical, _ = resolve_container_or_raise(name)
            if canonical not in seen:
                ordered.append(canonical)
                seen.add(canonical)
        return ordered, False

    if req.container_pattern is not None:
        # 显式 pattern → 同上，用户已掌控
        _validate_pattern(req.container_pattern)
        # canonical 容器名直接命中
        canonical_names = [
            p.name for p in _list_container_dirs()
            if _match_container(p.name, req.container_pattern, req.pattern_mode)
        ]
        # alias 命中（active / deprecated）→ 追加 canonical，避免遗漏旧名
        try:
            aliases = get_container_aliases_store().list_all()
        except Exception:  # pragma: no cover - 表故障不应阻塞 pattern 主路径
            aliases = []
        canonical_set = set(canonical_names)
        for row in aliases:
            if row.get("status") == "removed":
                continue
            if _match_container(row["alias"], req.container_pattern, req.pattern_mode):
                canonical = row["canonical"]
                if canonical not in canonical_set:
                    canonical_names.append(canonical)
                    canonical_set.add(canonical)
        return canonical_names, False

    validate_container_name(req.container)
    main, _ = resolve_container_or_raise(req.container)
    targets = [main]

    # v0.11.0 union 扩展：解析 union flag —— 显式优先；None 时走 ProfileSet 全局默认
    if req.union is False:
        return targets, False
    union_enabled = req.union is True or (req.union is None and _get_union_search_default())
    if not union_enabled:
        return targets, False

    # 主容器本身就是 _openai 镜像 → 不再追加（避免镜像找镜像的环）
    if main.endswith(_OPENAI_MIRROR_SUFFIX):
        return targets, False

    # sibling 不存在 → 不追加（不查不存在的容器，避免徒增 not_initialized 噪音）
    sibling = f'{main}{_OPENAI_MIRROR_SUFFIX}'
    existing_names = {p.name for p in _list_container_dirs()}
    if sibling not in existing_names:
        return targets, False

    # Phase 1 项2：sibling 目录存在但从未 embed（无 chunks 表）→ 软跳过。否则它会被
    # 压进 union 子查询返回 not_initialized，污染 per_container_status 并触发降级。
    # 日后 sibling embed 就绪 → 下次查询自动恢复双轨。
    if not _container_has_chunks_table(sibling):
        return targets, False

    targets.append(sibling)
    return targets, True


def _run_single_search(
    query: str,
    topk: int,
    container: str,
    timeout_s: int,
    embedding_override: str | None = None,
) -> tuple[CommandResponse, dict]:
    try:
        import lancedb
        try:
            from task_rag_runtime import embed_text, lancedb_dir
        except ModuleNotFoundError:
            from scripts.task_rag_runtime import embed_text, lancedb_dir

        db_path = str(lancedb_dir(container))
        db = lancedb.connect(db_path)
        
        # Helper to get table names
        def _table_names(db) -> list[str]:
            try:
                raw = db.list_tables()
            except Exception:
                return []
            names: list[str] = []
            for item in raw:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, (list, tuple)) and item:
                    names.append(str(item[0]))
                elif isinstance(item, dict):
                    names.append(str(item.get('name') or item.get('table_name') or ''))
                else:
                    name = getattr(item, 'name', '')
                    if name:
                        names.append(str(name))
            return [name for name in names if name]

        if 'chunks' not in set(_table_names(db)):
            try:
                table = db.open_table('chunks')
            except Exception:
                payload = {
                    'code': 'container_not_initialized',
                    'message': f"Container '{container}' has no searchable LanceDB table yet. Run /embed first.",
                    'container': container,
                    'initialized': False,
                    'results': [],
                }
                return CommandResponse(command=['in-process-search'], code=0, stdout=json.dumps(payload)), payload
        else:
            table = db.open_table('chunks')

        # Run embedding using custom override if specified
        if embedding_override:
            old_model = os.environ.get('TM_EMBEDDING_MODEL')
            os.environ['TM_EMBEDDING_MODEL'] = embedding_override
            try:
                vector = embed_text(query, mode='query')
            finally:
                if old_model is not None:
                    os.environ['TM_EMBEDDING_MODEL'] = old_model
                else:
                    os.environ.pop('TM_EMBEDDING_MODEL', None)
        else:
            vector = embed_text(query, mode='query')

        cleaned: list[dict[str, object]] = []
        for row in table.search(vector).limit(topk).to_list():
            item = dict(row)
            distance = item.pop('_distance', None)
            item.pop('vector', None)
            if distance is not None:
                item['score'] = float(distance)
            raw_meta = item.get('metadata')
            if isinstance(raw_meta, str):
                try:
                    parsed = json.loads(raw_meta)
                    item['metadata'] = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    item['metadata'] = {}
            cleaned.append(item)

        payload = {
            'code': 'ok',
            'container': container,
            'initialized': True,
            'results': cleaned,
        }
        return CommandResponse(command=['in-process-search'], code=0, stdout=json.dumps(payload)), payload
    except Exception as exc:
        logging.exception("Failed in-process LanceDB search; falling back to subprocess")
        result = run(
            [str(script_path('task_rag_search.py')), '--query', query, '--topk', str(topk), '--container', container],
            timeout_s,
            embedding_override=embedding_override,
            container=container,
            embed_mode='query',
        )
        try:
            payload = json.loads(result.stdout) if result.stdout.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return result, payload


def _resolve_search_rerank(req: SearchReq, targets: list[str]) -> tuple[bool, Any, str | None, int]:
    """Resolve the lightweight /search reranker policy.

    `/search` does not use LightRAG, so it must apply reranking explicitly after
    LanceDB returns candidate chunks. The route config is still the source of
    truth: `rerank.enabled` decides the default, while `req.rerank` can override
    it per request. `req.reranker_model` can override the route profile.
    """
    if not targets:
        return False, None, None, 0

    try:
        from embedding_registry import get_registry
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.embedding_registry import get_registry
    try:
        from reranker_registry import get_reranker_registry
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.reranker_registry import get_reranker_registry

    route = get_registry().resolve(targets[0])
    enabled = route.rerank_enabled if req.rerank is None else bool(req.rerank)
    profile_name = req.reranker_model or route.reranker
    candidate_topk = max(route.chunk_top_k, req.topk)
    if not enabled or not profile_name:
        return False, None, profile_name, candidate_topk

    rrk_registry = get_reranker_registry()
    # req.reranker_model 显式 override 时只用该单 profile —— route 的 fallback
    # 链是相对 route.reranker 定义的，对 override 不适用；否则展开 route 链。
    if req.reranker_model:
        chain = [rrk_registry.get_profile(req.reranker_model)]
    else:
        chain = [
            rrk_registry.get_profile(route.reranker),
            *(rrk_registry.get_profile(fb) for fb in route.reranker_fallbacks),
        ]
    rerank_func, _sig = rrk_registry.build_rerank_func(chain)
    return True, rerank_func, chain[0].name, candidate_topk


def _apply_search_rerank(
    query: str,
    hits: list[SearchHit],
    rerank_func: Any,
    topk: int,
) -> list[SearchHit]:
    """Rerank SearchHit objects with the existing Cohere-style reranker adapter.

    Reranker scores are relevance scores where larger is better. We keep
    `score` unchanged for backward compatibility with existing clients that
    treat it as LanceDB distance, and expose the reranker score separately.
    """
    if not hits:
        return []
    docs = [hit.text or hit.title or hit.source or '' for hit in hits]
    reranked = _asyncio.run(rerank_func(query, docs, top_n=topk))
    ranked: list[SearchHit] = []
    used: set[int] = set()
    for item in reranked:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item['index'])
            score = float(item['relevance_score'])
        except (KeyError, TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(hits) or idx in used:
            continue
        hit = hits[idx]
        hit.vectorScore = hit.score
        hit.rerankScore = score
        ranked.append(hit)
        used.add(idx)
    return ranked


def _collapse_media_caption_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """P1：把 `media_caption` 兄弟行折叠回其父媒体行。

    /embed-multimodal 一个媒体项落两行 —— 媒体原生向量行 + caption 文本向量行。
    文本 query 可能两行都命中。规则：
    - 父媒体行也在结果里 → 丢弃 caption 行，媒体行取两者较优（distance 更小）
      的分数；caption 命中信息记入媒体行 metadata。
    - 父媒体行未命中 → 保留 caption 行（它仍代表该媒体，metadata 带父引用）。

    依赖 caption 行 metadata 的 `parent_chunk_id`（由 /embed-multimodal 写入）。
    """
    media_by_chunk: dict[str, SearchHit] = {}
    for hit in hits:
        if hit.docType != 'media_caption' and hit.chunkId:
            media_by_chunk[hit.chunkId] = hit

    out: list[SearchHit] = []
    for hit in hits:
        if hit.docType == 'media_caption':
            parent_id = (hit.metadata or {}).get('parent_chunk_id')
            parent = media_by_chunk.get(parent_id) if isinstance(parent_id, str) else None
            if parent is not None:
                # 媒体行取较优分数（distance 越小越相关）。
                if hit.score is not None and (
                    parent.score is None or hit.score < parent.score
                ):
                    parent.score = hit.score
                if isinstance(parent.metadata, dict):
                    parent.metadata['caption_hit'] = True
                    if hit.score is not None:
                        parent.metadata['caption_score'] = hit.score
                continue  # caption 行折叠掉，不单独出现
        out.append(hit)
    return out


_DEFAULT_PER_CONTAINER_TIMEOUT_S = 12.0  # subprocess cold-start (py + lancedb + lightrag import) 实测 5-10s 不稳；v0.12 in-process 化后可降回 3s
_DEFAULT_QUERY_GATE_TIMEOUT_S = 30  # /query score-gate 的 top1 向量预检超时（仅 score-gate 启用时触发）


@app.post('/search', response_model=SearchResponse, dependencies=[Depends(verify_auth)])
def search(req: SearchReq) -> SearchResponse:
    targets, union_applied = _resolve_search_targets(req)
    rerank_enabled, rerank_func, reranker_name, candidate_topk = _resolve_search_rerank(req, targets)

    # 命中 0 个容器：返回空结果，但不算错误
    if not targets:
        return SearchResponse(
            status='ok',
            command=[],
            code=0,
            query=req.query,
            topk=req.topk,
            container=req.container,
            containers=[],
            per_container_status={},
            initialized=False,
            message='No container matched the request.',
            results=[],
            stdout='',
            stderr='',
            degraded=False,
            union_applied=False,
            rerank_applied=False,
            reranker=None,
        )

    # 跨容器召回时拉宽每容器 topk，避免被某个容器全占
    per_container_topk = req.topk if len(targets) == 1 else min(req.topk * len(targets), 100)
    if rerank_enabled:
        per_container_topk = min(max(per_container_topk, candidate_topk), 100)

    # v0.11.0：per-container timeout 上限仅在多容器（union）场景启用，避免破坏单容器
    # 长查询的向后兼容（旧默认 req.timeout_s=600s）。
    # Phase 1：默认改由 profiles.union_per_container_timeout_s 决定（默认 30.0s），
    # 旧 12.0 会把冷启动 + 网关 embedding 超时的主容器误杀成 timeout → 整条降级失败。
    per_container_timeout = req.per_container_timeout_s or _get_union_per_container_timeout()
    if len(targets) > 1:
        subproc_timeout = max(1, int(min(per_container_timeout, req.timeout_s)))
    else:
        subproc_timeout = req.timeout_s

    per_status: dict[str, str] = {}
    all_hits: list[SearchHit] = []
    last_command: list[str] = []
    last_stdout = ''
    last_stderr = ''
    any_initialized = False

    def _do(name: str) -> tuple[str, CommandResponse, dict]:
        cmd_result, payload = _run_single_search(
            req.query, per_container_topk, name, subproc_timeout,
            embedding_override=req.embedding_model,
        )
        return name, cmd_result, payload

    # 多容器走线程池 + future.result(timeout) 形成第二层 wall-clock 防线：
    # 即使 subprocess.run 内部 timeout 卡死（罕见但发生过），主进程也能继续。
    if len(targets) == 1:
        results_iter: list[tuple[str, CommandResponse, dict]] = [_do(targets[0])]
        timed_out_names: set[str] = set()
    else:
        results_iter = []
        timed_out_names = set()
        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as pool:
            future_to_name = {pool.submit(_do, name): name for name in targets}
            for fut in as_completed(future_to_name, timeout=None):
                name = future_to_name[fut]
                try:
                    # wall-clock 略宽于 subproc，给序列化/IPC 留一点
                    results_iter.append(fut.result(timeout=per_container_timeout + 1.0))
                except FuturesTimeoutError:
                    timed_out_names.add(name)
                    fut.cancel()
                except Exception as e:  # pragma: no cover - 防御性
                    timed_out_names.add(name)
                    per_status[name] = f'error: {type(e).__name__}'

    for name, cmd_result, payload in results_iter:
        last_command = cmd_result.command
        last_stdout = cmd_result.stdout
        last_stderr = cmd_result.stderr
        # subprocess timeout 也算 timeout（code=124），单独标记以便客户端区分
        if cmd_result.code == 124:
            per_status[name] = 'timeout'
            continue
        if cmd_result.code != 0:
            per_status[name] = f'error: exit {cmd_result.code}'
            continue
        code = payload.get('code')
        if code == 'container_not_initialized':
            per_status[name] = 'not_initialized'
            continue
        if code and code != 'ok':
            per_status[name] = f'error: {code}'
            continue
        per_status[name] = 'ok'
        any_initialized = True
        raw_results = payload.get('results') or []
        if not isinstance(raw_results, list):
            continue
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            item.setdefault('container', name)
            try:
                all_hits.append(SearchHit(**item))
            except Exception:  # pragma: no cover - defensive
                continue

    # 线程池 wall-clock timeout 命中但 subprocess 未返回 → 兜底标记
    for name in timed_out_names:
        per_status.setdefault(name, 'timeout')

    # LanceDB 距离越小越相关；None 视为最差
    all_hits.sort(key=lambda hit: (hit.score is None, hit.score if hit.score is not None else 0.0))

    # P1 caption 混合检索去重：媒体行与其 caption 兄弟行同时命中时折叠为一条。
    # 保留媒体行为规范结果，并取较优（distance 更小）的分数；caption 行单独命中
    # （父媒体行未进 topk）时原样保留 —— 它仍代表该媒体，且 metadata 带父引用。
    all_hits = _collapse_media_caption_hits(all_hits)
    # 折叠可能下调媒体行 distance → 重新按分数排序保证 topk 正确。
    all_hits.sort(key=lambda hit: (hit.score is None, hit.score if hit.score is not None else 0.0))

    # v0.11.0 dedup：union 同 (taskId, chunkId) 可能在两条镜像各召回一次，
    # 保留 score 更优（更小）的那条。dedup key 不含 vector / score，避免误杀。
    seen_keys: set[tuple[str, str]] = set()
    deduped: list[SearchHit] = []
    for hit in all_hits:
        hit.vectorScore = hit.score
        key = (hit.taskId or '', hit.chunkId or '')
        if key == ('', ''):
            # 缺标识符 → 不参与 dedup（保留，防止吞掉合法结果）
            deduped.append(hit)
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(hit)

    rerank_applied = False
    rerank_warning: str | None = None
    if rerank_enabled and rerank_func is not None and deduped:
        try:
            merged = _apply_search_rerank(req.query, deduped, rerank_func, req.topk)
            rerank_applied = True
        except Exception as exc:  # pragma: no cover - upstream/network dependent
            logger.warning(
                "/search rerank failed for profile=%s; returning vector results: %s",
                reranker_name, exc,
            )
            rerank_warning = f"rerank failed: {type(exc).__name__}"
            merged = deduped[: req.topk]
    else:
        merged = deduped[: req.topk]

    # Phase 1 项4：score-gate（**默认关闭，opt-in**）。度量是 LanceDB L2 距离（越小越
    # 相关，非相似度）→ 丢弃 score > 上界或 None 的 hit。请求级 score_threshold 优先于
    # profiles 默认；None 表示不启用（行为逐字节不变）；≤0 视为显式关闭。
    eff_threshold = (
        req.score_threshold if req.score_threshold is not None
        else _get_score_threshold_default()
    )
    blocked_low_score = 0
    if eff_threshold is not None and eff_threshold > 0:
        kept = [h for h in merged if h.score is not None and h.score <= eff_threshold]
        blocked_low_score = len(merged) - len(kept)
        merged = kept

    has_any_ok = any(s == 'ok' for s in per_status.values())
    # response.container 客户端无感原则：单容器入参保留客户端传入的原名；多容器
    # 入参（containers / pattern）则用 canonical 的第一个（targets 已经是 canonical）。
    if req.containers or req.container_pattern is not None:
        primary_container = targets[0] if targets else req.container
    else:
        primary_container = req.container

    # v0.11.0 degraded：任一容器超时 / 失败 / 未初始化都算降级（结果不完整）
    degraded = any(
        status != 'ok' for status in per_status.values()
    )
    degraded = degraded or rerank_warning is not None

    message = None
    if not has_any_ok and per_status:
        message = '; '.join(f'{name}: {status}' for name, status in per_status.items())
    elif rerank_warning:
        message = rerank_warning
    elif has_any_ok and degraded:
        # Phase 1 项1：部分容器成功的优雅降级 —— 仍有结果，不弹红条（前端据 status
        # 而非 message 判 error）。降级细节客户端读 per_container_status / is_degraded。
        message = None

    # Phase 1 项1：降级元数据。is_degraded 是 degraded 的 Agent 友好别名（同值双写）；
    # fallback_source 仅在「容器级降级」（有任一非 ok 容器）时标 partial_containers。
    # rerank-only 失败也算 degraded（结果不完整），但容器层无 partial → fallback_source=None，
    # 降级信息靠 is_degraded=True + message/reranker 表达，避免误标 partial_containers。
    fallback_source = (
        'partial_containers'
        if (has_any_ok and any(s != 'ok' for s in per_status.values()))
        else None
    )

    # Phase 1 项4：citation 投影（citation_enabled 默认开）。由最终 merged 派生，
    # 沿用 L2 距离语义；关闭时为 None。
    citations = None
    if _get_citation_enabled():
        citations = [
            Citation(
                chunkId=hit.chunkId,
                sourcePath=hit.sourcePath,
                section=hit.section,
                score=hit.score,
                container=hit.container,
            )
            for hit in merged
        ]

    # Phase 1：刻意保持 HTTP 200 —— 部分成功（有结果）走 200 + is_degraded body 标志；
    # 全失败也维持现状 200 + body status='error'（转 503 触及前端 ApiError，记 Phase 2）。
    return SearchResponse(
        status='ok' if has_any_ok else 'error',
        command=last_command,
        code=0 if has_any_ok else 1,
        query=req.query,
        topk=req.topk,
        container=primary_container,
        containers=targets,
        per_container_status=per_status,
        initialized=any_initialized,
        message=message,
        results=merged,
        stdout=last_stdout,
        stderr=last_stderr,
        degraded=degraded,
        is_degraded=degraded,
        fallback_source=fallback_source,
        citations=citations,
        blocked_low_score=blocked_low_score,
        union_applied=union_applied,
        rerank_applied=rerank_applied,
        reranker=reranker_name if rerank_applied else None,
    )


def _build_ingest_cmd(op: str, container: str, payload: dict) -> list[str]:
    """Map (op, container, payload) to the script invocation. Mirrors
    job_worker.default_command_resolver but stays usable from the request handler
    for the synchronous wait=True path."""
    if op in ('embed', 'ingest-memory'):
        cmd = [str(script_path('task_rag_lancedb_ingest.py')), '--container', container]
        memory_dir = payload.get('memory_dir')
        archive_dir = payload.get('archive_dir')
        if memory_dir:
            cmd += ['--memory-dir', str(memory_dir)]
        if archive_dir:
            cmd += ['--archive-dir', str(archive_dir)]
        return cmd
    if op == 'embed-backlog-retry':
        # backlog 静默重试：只重嵌该容器 backlog 里 waiting 的 chunk（断点续传）。
        return [
            str(script_path('task_rag_lancedb_ingest.py')),
            '--container', container,
            '--mode', 'embed-backlog-retry',
        ]
    if op == 'ingest-structured':
        cmd = [
            str(script_path('task_rag_structured_ingest.py')),
            '--container', container,
            '--input', str(payload.get('input_path', '')),
            '--doc-type', str(payload.get('doc_type', 'structured_json')),
        ]
        if payload.get('doc_id'):
            cmd += ['--doc-id', str(payload['doc_id'])]
        return cmd
    if op in ('ingest-document-text', 'ingest-document-file'):
        mode = 'text' if op == 'ingest-document-text' else 'file'
        cmd = [
            str(script_path('task_rag_graph_ingest.py')),
            '--container', container,
            '--mode', mode,
            '--input', str(payload.get('input_path', '')),
        ]
        if payload.get('description'):
            cmd += ['--description', str(payload['description'])]
        if payload.get('parse_method'):
            cmd += ['--parse-method', str(payload['parse_method'])]
        return cmd
    raise ValueError(f'unknown op: {op}')


def _enqueue_or_run(
    op: str,
    container: str,
    payload: dict,
    timeout_s: int,
    wait: bool,
    label: str = '',
    embedding_override: str | None = None,
    coalesce: bool = True,
) -> CommandResponse:
    """Dispatch one of the three ingest ops, choosing between three modes:

    1. wait=False (default): enqueue into persistent SQLite queue, return job_id.
       The single background worker drains the queue at a steady, host-friendly
       pace. Coalescing prevents duplicate enqueues for (op, container) — pass
       coalesce=False for ops whose payload carries a distinct per-call input
       (e.g. document ingestion) so concurrent posts are not collapsed/lost.

    2. wait=True with worker running: enqueue + poll queue until done/timeout.
       Lets a synchronous client share the same coalescing/backoff machinery
       as background callers.

    3. wait=True with worker disabled (typical in tests): bypass the queue and
       run the subprocess directly under GATE.acquire(). Preserves the legacy
       contract where `wait=true` returns the immediate subprocess result.
    """
    validate_container_name(container)
    queue = get_job_queue()

    worker_alive = bool(JOB_WORKER and JOB_WORKER.is_running)

    # 把 embedding_model 透传到 payload，worker 在落 job → resolver 时也能读到。
    # 即便当前 worker 不消费该字段（β 后续合并），队列向后兼容，多余 key 不影响。
    if embedding_override:
        payload = dict(payload)
        payload.setdefault('embedding_model', embedding_override)

    # Even on the queued path we still apply admit-gate so a flood under
    # genuine system pressure gets pushback (HTTP 503) instead of inflating
    # the SQLite queue. Coalescing already collapses duplicate (op, container);
    # this guards distinct-container fan-outs.
    _admit_or_503(container, op=op)
    max_pending = _env_int('TM_QUEUE_MAX_PENDING', 1000)

    if not wait:
        # Background mode: always enqueue, return job_id immediately.
        try:
            job_id = queue.enqueue(
                op=op, container=container, payload=payload,
                label=label or op, max_pending=max_pending,
                coalesce=coalesce,
            )
        except QueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail={'error': 'queue_full', 'op': op, 'reason': str(exc)},
                headers={'Retry-After': '60'},
            )
        return CommandResponse(
            command=[op, container],
            code=0,
            background=True,
            wait=False,
            pid=job_id,
            status='enqueued',
            note=f'Job enqueued (id={job_id}); the background worker will drain it.',
        )

    if worker_alive:
        # wait=True with worker: enqueue, then poll queue until job leaves pending/running.
        try:
            job_id = queue.enqueue(
                op=op, container=container, payload=payload,
                label=label or op, max_pending=max_pending,
                coalesce=coalesce,
            )
        except QueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail={'error': 'queue_full', 'op': op, 'reason': str(exc)},
                headers={'Retry-After': '60'},
            )
        deadline = time.time() + max(1, timeout_s)
        while time.time() < deadline:
            job = queue.get(job_id)
            if job is None:  # pragma: no cover - shouldn't happen
                break
            if job.status in ('done', 'failed', 'cancelled'):
                return _job_to_command_response(job)
            time.sleep(0.2)
        job = queue.get(job_id)
        if job is None:
            raise HTTPException(status_code=500, detail=f'job {job_id} disappeared')
        return _job_to_command_response(job, timed_out_wait=True)

    # wait=True without worker (tests, single-shot deployments): run inline.
    # GATE.acquire still enforces global single-flight + per-container locking.
    _admit_or_503(container, op=op)
    try:
        cmd = _build_ingest_cmd(op, container, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        with GATE.acquire(container):
            return run(cmd, timeout_s=timeout_s, embedding_override=embedding_override, container=container)
    except IngestBusyError as e:
        raise HTTPException(
            status_code=503,
            detail={'error': 'ingest_busy', 'reason': str(e)},
            headers={'Retry-After': '20'},
        )


def _job_to_command_response(job: Job, timed_out_wait: bool = False) -> CommandResponse:
    """Marshal a Job into the legacy CommandResponse shape.

    code semantics:
    - done    → result_code from subprocess (typically 0)
    - failed  → 1
    - cancelled → 1
    - running → 0 with status='running' (only when wait timed out)
    - pending → 0 with status='pending' (only when wait timed out)
    """
    if job.status == 'done':
        code = job.result_code if job.result_code is not None else 0
    elif job.status in ('failed', 'cancelled'):
        code = job.result_code if job.result_code is not None else 1
    else:
        code = 0
    note_parts = [f'job_id={job.id}', f'attempts={job.attempts}/{job.max_attempts}']
    if job.last_error:
        note_parts.append(f'last_error={job.last_error[:200]}')
    if timed_out_wait:
        note_parts.append('wait_timed_out_but_job_still_progressing')
    return CommandResponse(
        command=[job.op, job.container],
        code=code,
        stdout=job.last_error if job.status == 'done' and job.last_error else '',
        stderr=job.last_error if job.status != 'done' and job.last_error else '',
        background=False,
        wait=True,
        pid=job.id,
        status=job.status,
        note=' | '.join(note_parts),
    )


@app.post('/embed', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def embed(req: ContainerReq) -> CommandResponse:
    canonical, _ = resolve_container_or_raise(req.container)
    resp = _enqueue_or_run(
        op='embed',
        container=canonical,
        payload={},
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='embed',
        embedding_override=req.embedding_model,
    )
    # 说明静默重试 backlog：embedding 失败的对象不会丢，会进 backlog 后台重试。
    resp.note = f'{resp.note} {_BACKLOG_NOTE}'.strip() if resp.note else _BACKLOG_NOTE
    return resp


@app.post('/build-manifest', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def build_manifest(_req: ContainerReq) -> CommandResponse:
    return CommandResponse(command=[], code=0, status='deprecated', note='build-manifest was removed in LanceDB-only mode; use /embed.')


@app.post('/ingest-memory', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_memory(req: IngestMemoryReq) -> CommandResponse:
    canonical, _ = resolve_container_or_raise(req.container)
    payload = {}
    if req.memory_dir:
        payload['memory_dir'] = req.memory_dir
    if req.archive_dir:
        payload['archive_dir'] = req.archive_dir
    return _enqueue_or_run(
        op='ingest-memory',
        container=canonical,
        payload=payload,
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='ingest-memory',
        embedding_override=req.embedding_model,
    )


@app.get('/ingest-memory/contract', dependencies=[Depends(verify_auth)])
def ingest_contract() -> dict[str, object]:
    arch = detect_architecture()
    return {
        'mode': arch.name,
        'content_source': 'server-side-canonical-sources',
        'storage_location': 'Canonical LanceDB rows live under WORKSPACE/tasks/rag/containers/<container>/lancedb.',
        'retrieval_scope': 'Retrieval runs server-side against LanceDB only.',
        'notes': [
            'Use /ingest-memory/objects to persist typed objects into canonical server-side storage.',
            'Use /embed to rebuild task-card, markdown-memory, and typed-object rows into LanceDB.',
            'Use /ingest-structured for direct structured JSON-like ingest into LanceDB.',
        ],
    }


@app.post('/ingest-memory/objects', response_model=ClientIngestResponse, dependencies=[Depends(verify_auth)])
def ingest_objects(req: ClientIngestReq) -> ClientIngestResponse:
    validate_container_name(req.container)
    # alias 透传：客户端传 personal → 实际写 personal-notes；removed 直接 410。
    canonical, _ = resolve_container_or_raise(req.container)
    path = memory_objects_path(canonical)
    lines = []
    for obj in req.objects:
        payload = obj.model_dump(mode='json')
        payload['storedAt'] = int(time.time())
        lines.append(json.dumps(payload, ensure_ascii=False))
    # POSIX O_APPEND is atomic only for writes < PIPE_BUF (~4 KB). A single
    # large object can tear if two requests race; a per-container lock
    # serializes appends on the same JSONL file.
    container_lock = GATE._container_lock(canonical)  # noqa: SLF001 — intentional reuse
    with container_lock:
        with path.open('a', encoding='utf-8') as handle:
            for line in lines:
                handle.write(line + '\n')

    # auto_embed: enqueue a background embed job. The persistent queue coalesces
    # duplicate enqueues for the same container, so even posting many objects
    # in a tight loop only yields one pending embed job. The queue worker will
    # drain it later at a stable, host-friendly pace.
    if req.auto_embed:
        # 把 per-request 的 embedding 覆盖也带到 auto-embed payload，保持与显式 /embed 一致
        embed_payload: dict = {}
        if req.embedding_model:
            embed_payload['embedding_model'] = req.embedding_model
        try:
            get_job_queue().enqueue(
                op='embed', container=canonical, payload=embed_payload, label='auto-embed',
                max_pending=_env_int('TM_QUEUE_MAX_PENDING', 1000),
            )
        except QueueFullError as exc:
            # Don't fail the ingest — the objects ARE persisted. Just inform
            # the caller they need to /embed manually later.
            logger.warning('auto_embed dropped (queue full): %s', exc)
            index_hint = (
                'Memories persisted but queue is saturated; auto-embed skipped. '
                'Run /embed manually for this container later.'
            )
        else:
            index_hint = 'Embed job queued; the background worker will index this container shortly.'
    else:
        index_hint = 'Run /embed for this container to refresh LanceDB after storing new objects.'
    # 说明静默重试 backlog：后续 embedding 若失败，对象进 backlog 后台重试不丢失。
    index_hint = f'{index_hint} {_BACKLOG_NOTE}'
    # response.container 保留客户端传入的原名（无感原则）。
    return ClientIngestResponse(
        container=req.container,
        accepted=len(lines),
        stored_path=str(path),
        stored_paths=[str(path)],
        index_hint=index_hint,
    )


@app.post('/ingest-structured', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_structured(req: StructuredIngestReq) -> CommandResponse:
    canonical, _ = resolve_container_or_raise(req.container)
    payload = {
        'input_path': req.input_path,
        'doc_type': req.doc_type,
    }
    if req.doc_id:
        payload['doc_id'] = req.doc_id
    return _enqueue_or_run(
        op='ingest-structured',
        container=canonical,
        payload=payload,
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='ingest-structured',
        embedding_override=req.embedding_model,
    )


# --- 辅助函数 ---

_CONTAINER_NAME_RE = re.compile(r'^[a-zA-Z0-9_\-]+$')


def validate_container_name(name: str) -> None:
    """防路径遍历，仅允许字母数字、下划线和连字符。"""
    if not name or not _CONTAINER_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f'invalid container name: {name}')


_PATTERN_FORBIDDEN = re.compile(r'[\x00-\x1f/\\]')


def _validate_pattern(pattern: str) -> None:
    """限制 pattern 长度与字符集，避免路径分隔符与控制字符。"""
    if len(pattern) > 64:
        raise HTTPException(status_code=400, detail='pattern too long (max 64)')
    if _PATTERN_FORBIDDEN.search(pattern):
        raise HTTPException(status_code=400, detail='pattern contains forbidden characters')


def _match_container(name: str, pattern: str, mode: str) -> bool:
    """大小写不敏感的容器名匹配，支持 substring/prefix/glob 三种模式。"""
    needle = pattern.lower()
    target = name.lower()
    if mode == 'prefix':
        return target.startswith(needle)
    if mode == 'glob':
        return fnmatch.fnmatchcase(target, needle)
    return needle in target


def _list_container_dirs() -> list[Path]:
    """列出 containers 目录下的子目录，按名称排序。"""
    root = WS / 'tasks' / 'rag' / 'containers'
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)


_MEMORY_OBJECTS_MAX_BYTES = _env_int('TM_MEMORY_OBJECTS_MAX_BYTES', 256 * 1024 * 1024)  # 256 MB


def _check_memory_objects_size(path: Path, op: str) -> None:
    """Reject ops that would materialize a too-large JSONL into RAM.

    read/write_memory_objects loads the whole file into memory; on a 1.5 GB
    container a 500 MB JSONL would peak at ~3x file size during write.
    Tell the operator to compact rather than silently OOM.
    """
    if not path.exists():
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size > _MEMORY_OBJECTS_MAX_BYTES:
        raise HTTPException(
            status_code=507,  # Insufficient Storage
            detail={
                'error': 'memory_objects_too_large',
                'op': op,
                'path': str(path),
                'size_bytes': size,
                'max_bytes': _MEMORY_OBJECTS_MAX_BYTES,
                'hint': (
                    'Stream-rewrite or split this container before further '
                    'mutations. Override with TM_MEMORY_OBJECTS_MAX_BYTES if '
                    'your container memory limit allows.'
                ),
            },
        )


def read_memory_objects(container: str) -> list[dict]:
    """读取 container 下的 memory_objects.jsonl，返回 dict 列表。

    Iterates line-by-line so peak RAM is one parsed row, not the whole file.
    """
    path = memory_objects_path(container)
    _check_memory_objects_size(path, op='read_memory_objects')
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_memory_objects(container: str, rows: list[dict]) -> Path:
    """原子写入 memory_objects.jsonl（tmp + rename）。"""
    path = memory_objects_path(container)
    _check_memory_objects_size(path, op='write_memory_objects')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix('.jsonl.tmp')
    with tmp_path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    tmp_path.replace(path)
    return path


# --- 新端点 ---


@app.get('/export-connection-token', response_model=ConnectionTokenResponse, dependencies=[Depends(verify_auth)])
def export_connection_token(container: str = DEFAULT_CONTAINER) -> ConnectionTokenResponse:
    endpoint = os.environ.get('RAG_ADVERTISED_ENDPOINT', 'http://localhost:8711')
    payload = json.dumps({'endpoint': endpoint, 'api_key': RAG_API_KEY, 'container': container}, ensure_ascii=False)
    token = base64.b64encode(payload.encode('utf-8')).decode('ascii')
    pairing_auth, agent_onboarding = build_connection_onboarding(endpoint, container, RAG_API_KEY)
    return ConnectionTokenResponse(
        token=token,
        endpoint=endpoint,
        container=container,
        note='Base64-encoded connection token plus onboarding prompts and explicit pairing auth material for AI-assisted setup.',
        pairing_auth=pairing_auth,
        agent_onboarding=agent_onboarding,
    )


@app.get('/containers', dependencies=[Depends(verify_auth)])
def list_containers(pattern: str | None = None, mode: str = 'substring'):
    """列出容器，可选 pattern 模糊过滤。

    - pattern: 大小写不敏感的匹配字符串，留空时返回全部
    - mode: substring（默认）/ prefix / glob

    每个容器额外返回 ``metadata`` 字段（LEFT JOIN container_metadata 表，
    名字未注册的容器为 ``null``）。Phase 1.2 引入，向后兼容旧字段。
    """
    if mode not in ('substring', 'prefix', 'glob'):
        raise HTTPException(status_code=400, detail=f'invalid mode: {mode}')
    if pattern is not None:
        _validate_pattern(pattern)

    # 一次性把 container_metadata 全表读出，避免逐容器查库（N+1）。
    metadata_by_name: dict[str, dict] = {}
    try:
        for row in get_container_metadata_store().list_all():
            metadata_by_name[row['name']] = row
    except Exception:  # pragma: no cover - 元数据表故障不影响主列表
        logging.exception("container_metadata read failed; returning containers without metadata")
        metadata_by_name = {}

    # 反向 alias lookup：canonical → list[alias dict]，让 GET /containers 一目了然
    # 这个 canonical 容器被哪些旧名透传。removed 不入此表（避免误导，已 410 拒收）。
    aliases_by_canonical: dict[str, list[dict]] = {}
    try:
        for row in get_container_aliases_store().list_all():
            if row.get('status') == 'removed':
                continue
            aliases_by_canonical.setdefault(row['canonical'], []).append(row)
    except Exception:  # pragma: no cover - alias 表故障不影响主列表
        logging.exception("container_aliases read failed; returning containers without aliases")
        aliases_by_canonical = {}

    dirs = _list_container_dirs()
    result = []
    for p in dirs:
        if pattern and not _match_container(p.name, pattern, mode):
            continue
        # 统计对象数
        jsonl = p / 'memory_objects.jsonl'
        obj_count = 0
        last_mod = None
        if jsonl.exists():
            obj_count = sum(1 for line in jsonl.read_text(encoding='utf-8').splitlines() if line.strip())
            last_mod = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(jsonl.stat().st_mtime))
        # 检查索引
        lancedb_dir = p / 'lancedb'
        indexed = lancedb_dir.exists() and any(lancedb_dir.iterdir()) if lancedb_dir.exists() else False
        # 索引状态机：fresh / indexing / backlog / quota_blocked / error / stale / unknown
        status = _compute_container_index_status(p.name)
        result.append({
            'name': p.name,
            'objects': obj_count,
            'indexed': indexed,
            'last_modified': last_mod,
            'index_state': status['state'] if status else 'unknown',
            'metadata': metadata_by_name.get(p.name),
            # alias 反向 lookup：列出指向该 canonical 的全部非 removed alias 名。
            # 为空数组（而非 null）方便客户端简单 .length 判断。
            'aliases': [a['alias'] for a in aliases_by_canonical.get(p.name, [])],
        })
    return {'containers': result, 'count': len(result)}


# --- container_metadata: upsert + dump endpoints (Phase 1.2) ---

@app.post(
    '/containers/{name}/metadata',
    dependencies=[Depends(verify_auth)],
)
def upsert_container_metadata(name: str, payload: ContainerMetadataPayload):
    """upsert 容器元数据（命名规范 scope/entity/purpose + tags + policy）。

    - 首次写入时 created_at 落盘；updated_at 每次刷新。
    - tags / policy 在表里以 JSON string 形式存储，API 透明 encode/decode。
    - 容器目录不存在也可写 metadata —— 允许"先声明命名规范，后写入数据"。
    - 入参 name 若是 alias → 写到 canonical（避免双写漂移）。removed 抛 410。
    """
    validate_container_name(name)
    canonical, _ = resolve_container_or_raise(name)
    store = get_container_metadata_store()
    fields = payload.model_dump(exclude_none=True)
    row = store.upsert(canonical, **fields)
    return row


@app.get(
    '/containers/{name}/dump',
    dependencies=[Depends(verify_auth)],
)
def dump_container(name: str):
    """流式导出容器全部 row 为 NDJSON。

    用于 Phase 1.3 容器清理前的逐容器 JSONL dump，再人审 / 重导。
    - 每行一个 JSON 对象，对应 memory_objects.jsonl 中的一条原始记录。
    - 容器不存在或为空时返回空响应（200 + 0 字节）。
    - vector 列在 memory_objects.jsonl 中本就不存在；该端点只 dump JSONL，
      重导时由 /embed 重新生成 embedding。
    - 入参 name 若是 alias → 解析到 canonical 后 dump。removed 抛 410。
    """
    validate_container_name(name)
    canonical, _ = resolve_container_or_raise(name)
    path = (
        memory_objects_path(canonical)
        if (WS / 'tasks' / 'rag' / 'containers' / canonical).exists()
        else None
    )

    def gen():
        if path is None or not path.exists():
            return
        with path.open('r', encoding='utf-8') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (TypeError, ValueError):
                    # 跳过损坏行；不让单条坏 JSON 中断整流。
                    continue
                # dump 用于人审 / 重导，去掉向量字段（若 memory_objects 里也意外含 vector）。
                if isinstance(obj, dict):
                    obj.pop('vector', None)
                yield json.dumps(obj, ensure_ascii=False) + '\n'

    return StreamingResponse(gen(), media_type='application/x-ndjson')


# --- container_aliases: 透明 alias 路由管理端点 ---


from pydantic import BaseModel as _BaseModel  # 局部 import 避免顶部 import 链改动


class AliasPayload(_BaseModel):
    """upsert alias 请求体。canonical / reason 必填；status / notes 可选。"""

    alias: str
    canonical: str
    reason: str = ''
    status: str = 'active'  # active | deprecated | removed
    notes: str = ''


@app.post('/containers/aliases', dependencies=[Depends(verify_auth)])
def upsert_alias(payload: AliasPayload):
    """upsert 一条 alias 路由（admin only）。

    - alias / canonical 走 ``validate_container_name`` 校验（防路径遍历）。
    - status 必须 ∈ {active, deprecated, removed}；否则 400。
    - 写入后失效进程内 alias 缓存对应 key。
    """
    validate_container_name(payload.alias)
    validate_container_name(payload.canonical)
    if payload.status not in _ALIAS_VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=(
                f'invalid status {payload.status!r}; '
                f'expected one of {_ALIAS_VALID_STATUSES}'
            ),
        )
    try:
        row = get_container_aliases_store().upsert(
            alias=payload.alias,
            canonical=payload.canonical,
            reason=payload.reason,
            status=payload.status,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    _alias_cache_invalidate(payload.alias)
    return row


@app.get('/containers/aliases', dependencies=[Depends(verify_auth)])
def list_aliases():
    """列出所有 alias 路由（admin only）。"""
    try:
        rows = get_container_aliases_store().list_all()
    except Exception:  # pragma: no cover - 表故障
        logger.exception("alias table read failed")
        rows = []
    return {'aliases': rows, 'count': len(rows)}


@app.delete('/containers/aliases/{alias}', dependencies=[Depends(verify_auth)])
def delete_alias(alias: str):
    """物理删除一条 alias 路由（admin only）。

    删除的是 alias 表里的路由记录，**不**触碰 canonical 容器的物理数据。
    删除后该 alias 名会回退到"未注册"，下次写入会被当作新容器自动创建。
    """
    validate_container_name(alias)
    deleted = get_container_aliases_store().delete(alias)
    _alias_cache_invalidate(alias)
    if not deleted:
        raise HTTPException(status_code=404, detail=f'alias not found: {alias}')
    return {'deleted': True, 'alias': alias}


# --- 容器索引状态机 / embedding backlog 端点 ---

_EMBED_OPS_FOR_STATE = ('embed', 'ingest-memory', 'embed-backlog-retry')
_BACKLOG_LAST_ERROR_MAXLEN = 240


def _container_dir(name: str) -> Path:
    """容器目录路径（**不创建**，与 container_root() 不同 —— 状态查询不应产生副作用）。"""
    return WS / 'tasks' / 'rag' / 'containers' / name


def _container_object_count(name: str) -> int:
    """统计容器 memory_objects.jsonl 的对象数（无 index_state 记录时的回退来源）。"""
    jsonl = _container_dir(name) / 'memory_objects.jsonl'
    if not jsonl.exists():
        return 0
    try:
        return sum(
            1 for line in jsonl.read_text(encoding='utf-8').splitlines() if line.strip()
        )
    except OSError:  # pragma: no cover - defensive
        return 0


def _container_has_active_embed_job(name: str) -> bool:
    """该容器是否有 embed 类 job 处于 pending / running —— 推导 indexing 态。"""
    try:
        jobs = get_job_queue().list_jobs(container=name, limit=200)
    except Exception:  # pragma: no cover - defensive
        return False
    return any(
        j.op in _EMBED_OPS_FOR_STATE and j.status in ('pending', 'running')
        for j in jobs
    )


def _compute_container_index_status(name: str) -> dict | None:
    """组装单容器索引状态机视图。

    完全无记录（无 index_state 行、无 backlog、容器目录不存在）时返回 None —— 由
    调用方决定是否 404。state 由 compute_index_state 实时推导，不读缓存字段。
    """
    record = get_index_state_store().get(name)
    summary = get_backlog_store().summary(name)
    job_running = _container_has_active_embed_job(name)
    dir_exists = _container_dir(name).exists()

    backlog_active = summary['active']
    dead_count = summary['dead']

    if record is None and not dir_exists and backlog_active == 0 and dead_count == 0:
        return None

    if record is not None:
        # 子进程登记的权威计数（与 embedded_objects 同口径，避免与 jsonl 行数错配）。
        total_objects = record.total_objects
        embedded_objects = record.embedded_objects
        ever_embedded = record.last_embed_ok_at is not None
        last_embed_ok_at = record.last_embed_ok_at
        last_embed_attempt_at = record.last_embed_attempt_at
    else:
        # 从未 embed 过：对象数回退到 jsonl 行数，embedded=0 → 落 stale / unknown。
        total_objects = _container_object_count(name)
        embedded_objects = 0
        ever_embedded = False
        last_embed_ok_at = None
        last_embed_attempt_at = None

    state = compute_index_state(
        total_objects=total_objects,
        embedded_objects=embedded_objects,
        backlog_active=backlog_active,
        dead_count=dead_count,
        job_running=job_running,
        last_error_class=summary['last_error_class'],
        ever_embedded=ever_embedded,
    )
    return {
        'container': name,
        'state': state,
        'total_objects': total_objects,
        'embedded_objects': embedded_objects,
        'backlog_active': backlog_active,
        'backlog_counts': summary['counts'],
        'dead_count': dead_count,
        'job_running': job_running,
        'next_retry_at': summary['next_retry_at'],
        'last_error_class': summary['last_error_class'],
        'last_embed_ok_at': last_embed_ok_at,
        'last_embed_attempt_at': last_embed_attempt_at,
    }


def _backlog_item_to_response(item: BacklogItem) -> BacklogItemResponse:
    last_error = item.last_error
    if last_error is not None and len(last_error) > _BACKLOG_LAST_ERROR_MAXLEN:
        last_error = last_error[:_BACKLOG_LAST_ERROR_MAXLEN] + '…'
    return BacklogItemResponse(
        chunk_id=item.chunk_id,
        content_hash=item.content_hash,
        error_class=item.error_class,
        attempts=item.attempts,
        first_failed_at=item.first_failed_at,
        last_attempt_at=item.last_attempt_at,
        next_retry_at=item.next_retry_at,
        last_error=last_error,
        status=item.status,
        resolved_at=item.resolved_at,
    )


@app.get('/index-status', response_model=IndexStatusListResponse, dependencies=[Depends(verify_auth)])
def all_index_status() -> IndexStatusListResponse:
    """全容器索引状态机批量视图。

    容器并集来源：曾 embed 过的容器（container_index_state）∪ 有 backlog 的容器
    ∪ 当前 containers 目录。
    """
    names: set[str] = set()
    names.update(r.container for r in get_index_state_store().all())
    names.update(get_backlog_store().all_active_containers())
    names.update(p.name for p in _list_container_dirs())
    items: list[IndexStatusResponse] = []
    for name in sorted(names):
        status = _compute_container_index_status(name)
        if status is not None:
            items.append(IndexStatusResponse(**status))
    return IndexStatusListResponse(containers=items, count=len(items))


@app.get(
    '/containers/{name}/index-status',
    response_model=IndexStatusResponse,
    dependencies=[Depends(verify_auth)],
)
def container_index_status(name: str) -> IndexStatusResponse:
    """单容器索引状态机：state + 对象计数 + backlog 摘要 + next_retry_at。

    入参若是 alias → 解析到 canonical 后查询。removed 抛 410。
    """
    validate_container_name(name)
    canonical, _ = resolve_container_or_raise(name)
    status = _compute_container_index_status(canonical)
    if status is None:
        raise HTTPException(status_code=404, detail=f'container not found: {name}')
    return IndexStatusResponse(**status)


@app.get(
    '/containers/{name}/backlog',
    response_model=BacklogListResponse,
    dependencies=[Depends(verify_auth)],
)
def container_backlog(
    name: str,
    status: str | None = None,
    limit: int = 100,
) -> BacklogListResponse:
    """容器 embedding backlog 明细（含 dead 项）。

    - status：可选过滤 waiting / retrying / resolved / dead；非法值 → 400。
    - limit：1..500。
    - 每项 last_error 截断，避免响应体被长 traceback 撑大。
    - 入参若是 alias → 解析到 canonical 后查询。removed 抛 410。
    """
    validate_container_name(name)
    canonical, _ = resolve_container_or_raise(name)
    limit = max(1, min(500, int(limit)))
    store = get_backlog_store()
    try:
        items = store.list_items(canonical, status=status, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    summary = store.summary(canonical)
    # response.container 保留客户端入参，避免暴露 canonical（无感原则）。
    return BacklogListResponse(
        container=name,
        count=len(items),
        active=summary['active'],
        dead=summary['dead'],
        items=[_backlog_item_to_response(it) for it in items],
    )


@app.delete('/containers/{name}', response_model=ContainerDeleteResponse, dependencies=[Depends(verify_auth)])
def delete_container(name: str) -> ContainerDeleteResponse:
    """物理删除 canonical 容器。**不 resolve alias** —— 防止客户端传 alias 名误删
    背后的 canonical（多客户端共享 canonical 时副作用极大）。

    若传入名称是已注册 alias，返 400 让 caller 显式用 canonical 名重试。
    """
    validate_container_name(name)
    # 检测是否传入 alias —— 是则拒绝，要求 caller 显式用 canonical
    try:
        alias_row = get_container_aliases_store().resolve(name)
    except Exception:  # pragma: no cover - alias 表故障不应阻塞物理删除
        alias_row = None
    if alias_row is not None:
        raise HTTPException(
            status_code=400,
            detail={
                'error': 'cannot_delete_via_alias',
                'alias': name,
                'canonical': alias_row.get('canonical'),
                'hint': (
                    "DELETE /containers/{name} accepts only canonical container names "
                    f"to prevent accidental data loss. Retry with the canonical name "
                    f"'{alias_row.get('canonical')}' if you truly intend to delete it, "
                    "or DELETE /containers/aliases/{alias} to remove only the alias."
                ),
            },
        )
    target = WS / 'tasks' / 'rag' / 'containers' / name
    if not target.exists():
        raise HTTPException(status_code=404, detail=f'container not found: {name}')
    shutil.rmtree(target)
    return ContainerDeleteResponse(container=name, deleted=True, message=f'Container {name} deleted.')


@app.put(
    '/containers/{container}/memories/{memory_id}',
    response_model=MemoryUpdateResponse,
    dependencies=[Depends(verify_auth)],
)
def update_memory(container: str, memory_id: str, req: UpdateMemoryReq) -> MemoryUpdateResponse:
    validate_container_name(container)
    canonical, _ = resolve_container_or_raise(container)
    rows = read_memory_objects(canonical)
    found = False
    for row in rows:
        if row.get('id') == memory_id:
            found = True
            if req.text is not None:
                row['text'] = req.text
            if req.title is not None:
                row['title'] = req.title
            if req.source is not None:
                row['source'] = req.source
            if req.tags is not None:
                row['tags'] = req.tags
            if req.metadata is not None:
                row['metadata'] = req.metadata
            row['updatedAt'] = int(time.time())
            break
    if not found:
        raise HTTPException(status_code=404, detail=f'memory object not found: {memory_id}')
    write_memory_objects(canonical, rows)
    return MemoryUpdateResponse(
        container=container,
        id=memory_id,
        updated=True,
        message='Memory object updated.',
        index_hint='Run /embed for this container to refresh LanceDB after updating objects.',
    )


@app.delete(
    '/containers/{container}/memories/{memory_id}',
    response_model=MemoryDeleteResponse,
    dependencies=[Depends(verify_auth)],
)
def delete_memory(container: str, memory_id: str) -> MemoryDeleteResponse:
    validate_container_name(container)
    canonical, _ = resolve_container_or_raise(container)
    rows = read_memory_objects(canonical)
    new_rows = [r for r in rows if r.get('id') != memory_id]
    if len(new_rows) == len(rows):
        raise HTTPException(status_code=404, detail=f'memory object not found: {memory_id}')
    write_memory_objects(canonical, new_rows)
    return MemoryDeleteResponse(
        container=container,
        id=memory_id,
        deleted=True,
        message='Memory object deleted.',
    )


@app.get('/admin/system-health', dependencies=[Depends(verify_auth)])
async def admin_system_health(container: str | None = None) -> dict:
    """运维诊断端点：返回 /health 公开字段 + 全部敏感诊断信息。

    与公开 /health 的关系：
    - /health 是 LB-style 最小响应：状态布尔、压力标签、脱敏 warnings；
    - 本端点是 /health 的超集，额外暴露：
        * 容器列表 (`available_containers`)、绝对路径 (`workspace`, `containers_root`)
        * 完整配置摘要 (`configuration_guide`, `modules` 含 required/missing keys)
        * 原始系统快照 (`system` 含 cgroup_mem 数值) + 阈值 (`thresholds`)
        * 队列计数 (`queue_stats`) + 后台 job 明细 (`background_jobs`)
        * 未脱敏的 `warnings`（含触发数值，便于排"为什么 ingest 被拒"）

    出于安全考虑这些信息只对持有 RAG_API_KEY 的运维方公开。
    """
    state = await _collect_health_state(container)
    # admin 视图：取全部字段，但把 'public_warnings' 和 'sensitive_warnings' 折叠成单一 warnings
    out = {k: v for k, v in state.items() if k not in ('public_warnings', 'sensitive_warnings')}
    out['warnings'] = state['sensitive_warnings']
    # 兼容旧 admin 调用方：保留 admit_ok / gate_config 字段名
    out['admit_ok'] = state['accepting_ingest']
    out['gate_config'] = state['thresholds']
    out['background_jobs'] = BG_TRACKER.list_active()
    out['background_max_alive'] = BG_TRACKER.max_alive
    out['retry_cooldown_sec'] = RETRY_LIMITER.cooldown_sec
    return out


@app.get('/admin/profiles', dependencies=[Depends(verify_auth)])
async def admin_profiles() -> dict:
    """列出已加载的 embedding / reranker profile + 容器路由表。

    所有 api_key 必须脱敏：只返回 `api_key_configured: bool`，**绝不**把真值写入响应。
    这是 /admin/system-health 已有的安全契约的延伸 — 鉴权端点也禁止直接吐 secret。
    """
    try:
        try:
            from embedding_registry import get_registry
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import get_registry
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'embedding registry unavailable: {exc}')
    try:
        reg = get_registry()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'failed to load profiles: {exc}')
    ps = reg.profiles
    return {
        'embeddings': [
            {
                'name': p.name,
                'provider': p.provider,
                'model': p.model,
                'dim': p.dim,
                'base_url': p.base_url,
                'api_key_configured': bool(p.api_key),
                'max_token_size': p.max_token_size,
                'request_dim': p.request_dim,
                'timeout_s': p.timeout_s,
                'max_retries': p.max_retries,
            }
            for p in ps.embeddings.values()
        ],
        'rerankers': [
            {
                'name': r.name,
                'provider': r.provider,
                'model': r.model,
                'base_url': r.base_url,
                'api_key_configured': bool(r.api_key),
                'timeout_s': r.timeout_s,
                'min_score': r.min_score,
            }
            for r in ps.rerankers.values()
        ],
        'routes': [
            {
                'match': matcher,
                'embedding': route.embedding,
                'embedding_fallbacks': list(route.embedding_fallbacks),
                'reranker': route.reranker,
                'rerank_enabled': route.rerank_enabled,
                'chunk_top_k': route.chunk_top_k,
                'top_k': route.top_k,
            }
            for matcher, route in ps.routes
        ],
        'default_route': {
            'embedding': ps.default_route.embedding,
            'embedding_fallbacks': list(ps.default_route.embedding_fallbacks),
            'reranker': ps.default_route.reranker,
            'rerank_enabled': ps.default_route.rerank_enabled,
            'chunk_top_k': ps.default_route.chunk_top_k,
            'top_k': ps.default_route.top_k,
        } if ps.default_route else None,
    }


@app.post('/admin/probe-embedding', dependencies=[Depends(verify_auth)])
async def admin_probe_embedding(profile: str) -> dict:
    """对一条 embedding profile 做单 token 活检。返回 latency + dim。

    返回结构：
      - ok=true  → {ok, profile, latency_ms, dim, breaker_reset}
      - ok=false → {ok, profile, latency_ms, error, breaker_reset=false}
        （error 截断 200 字符防泄漏）
    未知 profile name → 404；不允许吞掉客户端配置错误。

    v0.9.0：探活成功后**显式重置该 profile 的 circuit breaker** — 让运维
    能手动「拔保险丝」而不必等 30s 自动 half-open。`breaker_reset` 字段
    明确告知调用方是否真的清掉了一条已存在的 breaker 状态（False 表示
    该 profile 从未触发过 breaker，等价于 no-op）。失败时不重置，让
    breaker 计数照常累加。
    """
    try:
        try:
            from embedding_registry import (
                get_registry,
                _http_embed,
                reset_breaker,
                _breaker_mark_failure,
                _is_fallback_eligible,
            )
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.embedding_registry import (
                get_registry,
                _http_embed,
                reset_breaker,
                _breaker_mark_failure,
                _is_fallback_eligible,
            )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f'embedding registry unavailable: {exc}')
    reg = get_registry()
    try:
        p = reg.get_profile(profile)
    except KeyError:
        raise HTTPException(status_code=404, detail=f'profile {profile!r} not found')
    t0 = time.monotonic()
    try:
        result = await _http_embed(p, ['probe'])
        # numpy 或 list-of-list 都兼容：取第一条 embedding 的长度作为 dim
        try:
            dim = int(result.shape[1])  # numpy ndarray 路径
        except AttributeError:
            dim = len(result[0]) if result else 0
        # 探活成功 → 拔保险丝。reset_breaker 返回 True 表示曾经 open，
        # False 表示从未触发；两者都视为「成功」，但客户端能区分语义
        breaker_reset = reset_breaker(profile)
        return {
            'ok': True,
            'profile': profile,
            'latency_ms': int((time.monotonic() - t0) * 1000),
            'dim': dim,
            'breaker_reset': breaker_reset,
        }
    except Exception as exc:
        # 探活失败 → 不重置 breaker，并按 fallback-eligible 语义累加计数。
        # 4xx 非 429（用户配置错）不计入 breaker — 与 _http_embed_with_fallback
        # 行为一致：「我们配错了」不该污染上游可用性指标。
        if _is_fallback_eligible(exc):
            _breaker_mark_failure(profile)
        return {
            'ok': False,
            'profile': profile,
            'latency_ms': int((time.monotonic() - t0) * 1000),
            'error': str(exc)[:200],
            'breaker_reset': False,
        }


@app.get('/jobs', dependencies=[Depends(verify_auth)])
def list_jobs(
    status: str | None = None,
    container: str | None = None,
    limit: int = 50,
) -> dict:
    """List queue contents. Optional filters: status (pending/running/done/failed/cancelled),
    container (exact match), limit (1..500)."""
    if container:
        validate_container_name(container)
    limit = max(1, min(500, int(limit)))
    try:
        jobs = get_job_queue().list_jobs(status=status, container=container, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    stats = get_job_queue().stats()
    worker_running = bool(JOB_WORKER and JOB_WORKER.is_running)
    return {
        'jobs': [j.to_dict() for j in jobs],
        'stats': stats,
        'worker_running': worker_running,
    }


@app.get('/jobs/{job_id}', response_model=JobStatusResponse, dependencies=[Depends(verify_auth)])
def job_status(job_id: int) -> JobStatusResponse:
    """Look up a queue job by id. The legacy 'pid' field in the response holds
    the queue job id (kept for backward-compat with old clients that read it)."""
    job = get_job_queue().get(job_id)
    if job is None:
        return JobStatusResponse(pid=job_id, running=False, exit_code=None,
                                 message=f'Job {job_id} not found.')
    running = job.status in ('pending', 'running')
    exit_code = job.result_code if job.status == 'done' else None
    parts = [f'status={job.status}', f'attempts={job.attempts}/{job.max_attempts}']
    if job.last_error:
        parts.append(f'last_error={job.last_error[:200]}')
    return JobStatusResponse(
        pid=job_id,
        running=running,
        exit_code=exit_code,
        message=' '.join(parts),
    )


@app.delete('/jobs/{job_id}', dependencies=[Depends(verify_auth)])
def cancel_job(job_id: int) -> dict:
    """Cancel a pending job. Running jobs cannot be cancelled mid-flight."""
    cancelled = get_job_queue().cancel(job_id)
    if not cancelled:
        job = get_job_queue().get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f'Job {job_id} not found.')
        raise HTTPException(
            status_code=409,
            detail=f'Job {job_id} is in status {job.status!r}; only pending jobs can be cancelled.',
        )
    return {'cancelled': True, 'job_id': job_id}


def _require_lightrag_ready() -> None:
    arch = detect_architecture()
    module = arch.modules['lightrag']
    if module.ready:
        return
    pkg = '' if module.package_available else ' lightrag package not installed.'
    missing = f' Missing keys: {", ".join(module.missing_keys)}' if module.missing_keys else ''
    raise HTTPException(status_code=503, detail=f'LightRAG not available.{pkg}{missing}')


def _inbox_dir(container: str) -> Path:
    """container 的异步入库暂存目录，建图 CLI 子进程从这里读输入并在成功后清理。"""
    d = WS / 'tasks' / 'rag' / 'containers' / container / '_inbox'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stage_inbox_text(container: str, text: str) -> Path:
    """把 /documents/text 的正文落到 container 的 _inbox，返回绝对路径。

    用 uuid 命名（不依赖 job_id —— job_id 要 enqueue 后才有，会形成先有鸡先有
    蛋的依赖）。"""
    path = _inbox_dir(container) / f'text-{uuid.uuid4().hex}.txt'
    path.write_text(text, encoding='utf-8')
    return path.resolve()


@app.post('/documents/text', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_document_text(req: DocumentTextReq) -> CommandResponse:
    """把纯文本异步入库到 container 知识图谱。

    建图（LightRAG ainsert）会跑数十秒到数分钟 —— 同步等待会撞边缘代理的
    100s 超时。这里把正文落到 _inbox 后立即入队并返回 job 标识；后台 worker
    驱动 task_rag_graph_ingest.py 子进程完成建图。轮询 GET /jobs/{pid} 查进度。
    """
    validate_container_name(req.container)
    _require_lightrag_ready()
    # embedding_model override 在建图路径暂不生效（需 registry-based cache key
    # 才能切换 instance）。接受字段以保持 API 兼容，指定时记一行 warning。
    if req.embedding_model:
        logger.warning(
            'documents/text received embedding_model=%r but the graph-ingest path '
            'does not honor per-request override yet; using route default.',
            req.embedding_model,
        )
    input_path = _stage_inbox_text(req.container, req.text)
    payload: dict[str, Any] = {'input_path': str(input_path)}
    if req.description:
        payload['description'] = req.description
    return _enqueue_or_run(
        op='ingest-document-text',
        container=req.container,
        payload=payload,
        timeout_s=300,
        wait=False,
        label='ingest-document-text',
        embedding_override=req.embedding_model,
        coalesce=False,
    )


_MAX_UPLOAD_BYTES = int(os.environ.get('MAX_UPLOAD_BYTES', str(200 * 1024 * 1024)))  # 200 MB


def _sanitize_upload_filename(raw: str | None) -> str:
    """校验并规范化上传文件名，不合法即抛 400。"""
    name = os.path.basename(raw or '')
    if not name or name in ('.', '..'):
        raise HTTPException(status_code=400, detail='invalid filename: empty or reserved')
    if '\x00' in name or '/' in name or '\\' in name:
        raise HTTPException(status_code=400, detail=f'invalid filename: {name!r}')
    return name


async def _stage_inbox_upload(container: str, filename: str, file: UploadFile) -> Path:
    """把上传文件流式落到 container 的 _inbox，做大小上限校验，返回绝对路径。

    流式写盘（1 MB 分块）避免把大文件整体读进内存；超过 MAX_UPLOAD_BYTES 即
    413 并清理半成品。"""
    inbox = _inbox_dir(container)
    saved = inbox / f'file-{uuid.uuid4().hex}-{filename}'
    # 防御性：验证 resolve 后仍在 inbox 内
    if not str(saved.resolve()).startswith(str(inbox.resolve()) + os.sep):
        raise HTTPException(status_code=400, detail='invalid filename: path traversal')
    total = 0
    try:
        with saved.open('wb') as f:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f'file exceeds max upload size {_MAX_UPLOAD_BYTES} bytes',
                    )
                f.write(chunk)
    except Exception:
        saved.unlink(missing_ok=True)
        raise
    return saved.resolve()


async def _ingest_uploaded_document(
    container: str,
    file: UploadFile,
    parse_method: str | None,
    embedding_model: str | None,
) -> CommandResponse:
    """/documents/file 与 /documents/upload 的共享异步入库逻辑。

    把上传文件落到 _inbox 后立即入队（op=ingest-document-file）并返回 job 标识；
    后台 worker 驱动 task_rag_graph_ingest.py 子进程跑 RAG-Anything 解析 + 建图，
    避免同步等待数十秒到数分钟撞边缘代理 100s 超时。
    """
    validate_container_name(container)
    _require_lightrag_ready()
    if get_raganything is None:
        raise HTTPException(status_code=503, detail='raganything package not installed; rebuild with multimodal flavor.')
    filename = _sanitize_upload_filename(file.filename)
    if embedding_model:
        logger.warning(
            'documents/file received embedding_model=%r but the graph-ingest path '
            'does not honor per-request override yet; using route default.',
            embedding_model,
        )
    saved = await _stage_inbox_upload(container, filename, file)
    payload: dict[str, Any] = {'input_path': str(saved)}
    if parse_method:
        payload['parse_method'] = parse_method
    return _enqueue_or_run(
        op='ingest-document-file',
        container=container,
        payload=payload,
        timeout_s=600,
        wait=False,
        label='ingest-document-file',
        embedding_override=embedding_model,
        coalesce=False,
    )


@app.post('/documents/file', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
async def ingest_document_file(
    container: str = Form(...),
    file: UploadFile = File(...),
    parse_method: str | None = Form(default=None),
    embedding_model: str | None = Form(default=None),
) -> CommandResponse:
    """多模态文档异步入库：PDF / Office / 图片 / HTML / Markdown 等。

    底层走 RAGAnything.process_document_complete → mineru parser → LightRAG，
    与 /documents/text 写入同一容器知识图谱。轮询 GET /jobs/{pid} 查进度。
    """
    return await _ingest_uploaded_document(container, file, parse_method, embedding_model)


@app.post('/documents/upload', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
async def upload_document(
    container: str = Form(...),
    file: UploadFile = File(...),
    parse_method: str | None = Form(default=None),
    embedding_model: str | None = Form(default=None),
) -> CommandResponse:
    """/documents/file 的别名路由，行为完全一致（异步入队多模态文档）。"""
    return await _ingest_uploaded_document(container, file, parse_method, embedding_model)


_EMBED_MM_MAX_BYTES = int(os.environ.get('EMBED_MM_MAX_BYTES', str(20 * 1024 * 1024)))


def _resolve_gemini_native_profile(container: str):
    """解析 container 路由 → embedding profile，强制要求 gemini_native provider。

    多模态 embedding 只能走 Gemini 原生 :embedContent 协议；命中非 gemini_native
    的 profile（如 openai_compatible）直接 400，不静默降级。
    """
    try:
        from embedding_registry import get_registry
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.embedding_registry import get_registry
    try:
        reg = get_registry()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f'failed to load profiles: {exc}')
    route = reg.resolve(container)
    profile = reg.get_profile(route.embedding)
    if profile.provider != 'gemini_native':
        raise HTTPException(
            status_code=400,
            detail=(
                f'container {container!r} routes to embedding profile '
                f'{profile.name!r} (provider={profile.provider!r}); multimodal '
                'ingest requires a gemini_native profile (e.g. gemini-embedding-2). '
                'Add a profiles.yaml route for this container first.'
            ),
        )
    return profile


def _resolve_caption_vlm_chain(container: str, embed_profile) -> list:
    """解析 /embed-multimodal caption 生成的 VLM fallback 链 [primary, *fallbacks]。

    优先用 route 配置的 VLM 链；route 未配 VLM 时合成单元素链 —— 复用
    gemini_native embedding profile 的 relay base_url + token，caption 模型用
    `GE2_CAPTION_VLM_MODEL` 默认（与历史 /embed-multimodal caption 行为一致）。
    """
    try:
        from embedding_registry import get_registry
        from profiles_loader import VLMProfile
        import gemini_native_embed as _gne
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.embedding_registry import get_registry
        from scripts.profiles_loader import VLMProfile
        import scripts.gemini_native_embed as _gne

    reg = get_registry()
    route = reg.resolve(container)
    if route.vlm is None:
        # route 未配 VLM —— caption 复用 embedding relay，模型走 caption 默认。
        return [VLMProfile(
            name='caption-default',
            model=_gne._DEFAULT_CAPTION_MODEL,
            base_url=embed_profile.base_url,
            api_key=embed_profile.api_key,
            provider='gemini_native',
            timeout_s=embed_profile.timeout_s,
            max_retries=embed_profile.max_retries,
        )]
    vlms = reg.profiles.vlms
    return [vlms[route.vlm], *(vlms[fb] for fb in route.vlm_fallbacks)]


@app.post('/embed-multimodal', dependencies=[Depends(verify_auth)])
async def embed_multimodal(
    container: str = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    doc_id: str | None = Form(default=None),
) -> dict:
    """单个媒体文件 → gemini-embedding-2 原生多模态 embedding → 一条 LanceDB 向量行。

    与 /documents/file 的区别：/documents/file 走 RAGAnything + mineru 解析 +
    LightRAG 知识图谱（图片靠 VLM 转写，无音 / 视频路径）；本端点把媒体原始
    字节直接经 Gemini 原生 :embedContent 算成统一向量空间的向量，一个媒体项
    落一条 chunks 行，/search 即可向量检回 —— 路径最短，不与旧多模态管线纠缠。

    要求目标 container 路由命中的 embedding profile 为 gemini_native provider。
    caption 可选：给出时与媒体作为联合 part 一起送入 embedContent，并作为该行
    可读文本；缺省时文本回退为文件名。
    """
    validate_container_name(container)
    filename = _sanitize_upload_filename(file.filename)
    profile = _resolve_gemini_native_profile(container)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail='uploaded file is empty')
    if len(raw) > _EMBED_MM_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f'media exceeds inline embed limit {_EMBED_MM_MAX_BYTES} bytes',
        )

    try:
        from gemini_native_embed import (
            SUPPORTED_MIMES, embed_parts_async, generate_media_caption_async,
            guess_mime, inline_part, modality_of, text_part,
        )
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.gemini_native_embed import (
            SUPPORTED_MIMES, embed_parts_async, generate_media_caption_async,
            guess_mime, inline_part, modality_of, text_part,
        )
    mime = guess_mime(filename, file.content_type)
    if mime not in SUPPORTED_MIMES:
        raise HTTPException(
            status_code=415,
            detail=(f'unsupported media type {mime!r} for {filename!r}; '
                    f'supported: {sorted(SUPPORTED_MIMES)}'),
        )

    # 媒体原生多模态向量 —— 媒体 part 直接 embed，不套任何文本前缀（P0）。
    try:
        vector = await embed_parts_async(profile, [inline_part(mime, raw)])
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f'gemini multimodal embed failed: {str(exc)[:300]}')

    # P1 caption 混合检索：经 Gemini relay VLM 为媒体生成文字 caption，按 route
    # 的 VLM fallback 链逐条尝试（主 VLM 挂掉切下一条）。
    # best-effort —— caption 链路全挂（NoUpstreamAvailable）或其它异常都不阻塞
    # 媒体行落库（媒体原生向量仍可检回）。用户显式 caption 优先于自动生成。
    vlm_caption: str | None = None
    if not caption:
        try:
            from model_fallback import CATEGORY_VLM, run_with_fallback
        except ModuleNotFoundError:  # pragma: no cover - package import path
            from scripts.model_fallback import CATEGORY_VLM, run_with_fallback
        vlm_chain = _resolve_caption_vlm_chain(container, profile)

        async def _caption_executor(vlm_profile):
            return await generate_media_caption_async(
                vlm_profile, mime, raw, model=vlm_profile.model,
            )

        try:
            vlm_caption = await run_with_fallback(
                CATEGORY_VLM, vlm_chain, _caption_executor,
            )
        except Exception as exc:
            # NoUpstreamAvailable（全链挂）也落这里 —— caption 缺省为 None。
            logger.warning(
                'embed-multimodal caption generation failed for %r: %s',
                filename, str(exc)[:200])
    effective_caption = caption or vlm_caption
    caption_source = 'user' if caption else ('vlm' if vlm_caption else 'none')

    stable = doc_id or f'mm-{hashlib.sha1(f"{container}/{filename}".encode()).hexdigest()[:16]}'
    modality = modality_of(mime)
    media_chunk_id = f'{stable}#multimodal'
    media_row = {
        'chunkId': media_chunk_id,
        'taskId': stable,
        'docType': 'multimodal',
        'sourcePath': filename,
        'section': modality,
        'text': effective_caption or filename,
        'title': filename,
        'source': 'embed-multimodal',
        'tags': [modality],
        'metadata': {
            'modality': modality,
            'mime_type': mime,
            'size_bytes': len(raw),
            'filename': filename,
            'caption': effective_caption or '',
            'caption_source': caption_source,
        },
        'embedding_model': profile.model,
        'embedding_dim': int(len(vector)),
        'embedding_profile': profile.name,
        'vector': vector.tolist(),
    }
    rows = [media_row]

    # caption 兄弟行：caption 经 document 侧文本 embedding 存为同容器另一行，
    # 关联父媒体行 id。文本 query 既能命中媒体原生向量、又能命中 caption 文本
    # 向量 —— 双表征闭合模态间隙。caption embedding 失败同样 best-effort。
    caption_chunk_id: str | None = None
    if effective_caption:
        try:
            cap_vec = await embed_parts_async(
                profile, [text_part(effective_caption)],
                mode='document', title=filename,
            )
            caption_chunk_id = f'{stable}#caption'
            rows.append({
                'chunkId': caption_chunk_id,
                'taskId': stable,
                'docType': 'media_caption',
                'sourcePath': filename,
                'section': modality,
                'text': effective_caption,
                'title': filename,
                'source': 'embed-multimodal-caption',
                'tags': [modality, 'caption'],
                'metadata': {
                    'modality': modality,
                    'mime_type': mime,
                    'caption_source': caption_source,
                    # /search 去重靠这两个父引用把 caption 行折叠回媒体行。
                    'parent_chunk_id': media_chunk_id,
                    'parent_doc_id': stable,
                },
                'embedding_model': profile.model,
                'embedding_dim': int(len(cap_vec)),
                'embedding_profile': profile.name,
                'vector': cap_vec.tolist(),
            })
        except Exception as exc:
            logger.warning(
                'embed-multimodal caption embedding failed for %r: %s',
                filename, str(exc)[:200])

    try:
        from task_rag_lancedb_ingest import ingest_precomputed_rows
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.task_rag_lancedb_ingest import ingest_precomputed_rows
    summary = ingest_precomputed_rows(container, rows)

    return {
        'status': 'ok',
        'container': container,
        'doc_id': stable,
        'chunk_id': media_chunk_id,
        'caption_chunk_id': caption_chunk_id,
        'caption': effective_caption or '',
        'caption_source': caption_source,
        'modality': modality,
        'mime_type': mime,
        'size_bytes': len(raw),
        'embedding_profile': profile.name,
        'embedding_model': profile.model,
        'embedding_dim': int(len(vector)),
        **summary,
    }




@app.post('/query', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def query_rag(req: QueryReq) -> QueryResponse:
    validate_container_name(req.container)
    canonical, _ = resolve_container_or_raise(req.container)

    # Phase 1 项4：score-gate（**默认关闭，opt-in**）。在动用 LightRAG / LLM 前用
    # 一次轻量 top1 向量预检（L2 距离，越小越相关）：若 top1 距离 > 上界 → 直接返回
    # score_gated，不调 LLM（也不触发 _require_lightrag_ready 的 503）。
    # eff_threshold 为 None 时整段跳过，行为与现状逐字节一致。
    eff_threshold = (
        req.score_threshold if req.score_threshold is not None
        else _get_score_threshold_default()
    )
    if eff_threshold is not None and eff_threshold > 0:
        # 阻塞型预检（embedding HTTP + LanceDB IO + 可能 subprocess）放线程池，
        # 否则在 async 事件循环里同步调用会冻结整个 loop（阻塞其他并发请求）。
        _, top_payload = await asyncio.to_thread(
            _run_single_search,
            req.query, 1, canonical, _DEFAULT_QUERY_GATE_TIMEOUT_S,
            embedding_override=req.embedding_model,
        )
        # 区分三类：容器未初始化 → 诚实返回 not_initialized（让 Agent 先 /embed，
        # 而非伪装成「分数太低」）；真后端/网关错误 → 不静默 score_gated，放行到下游
        # _require_lightrag_ready / _admit_or_503 给真实状态/503；仅 ok 时才按距离判 gate。
        code = top_payload.get('code')
        if code == 'container_not_initialized':
            return QueryResponse(
                status='not_initialized',
                query=req.query,
                container=req.container,
                answer='',
                mode=req.mode,
                top_score=None,
            )
        if code == 'ok':
            top_hits = top_payload.get('results') or []
            top_score = None
            if isinstance(top_hits, list) and top_hits and isinstance(top_hits[0], dict):
                raw = top_hits[0].get('score')
                top_score = float(raw) if raw is not None else None
            # top_score 非 None 且超阈值 → 真低相关，gate 拦；top_score is None（已初始化但
            # 无任何 hit / 空容器）按可接受策略仍 score_gated（无可喂给 LLM 的上下文）。
            if top_score is None or top_score > eff_threshold:
                return QueryResponse(
                    status='score_gated',
                    query=req.query,
                    container=req.container,
                    answer='',
                    mode=req.mode,
                    top_score=top_score,
                )
        # code 既非 'ok' 也非 'container_not_initialized'（真后端/网关错误）→ 不拦，
        # 继续放行到下游既有 _require_lightrag_ready / _admit_or_503 路径。

    _require_lightrag_ready()
    _admit_or_503(canonical, op='query')
    # Phase 2：rerank / chunk_top_k 字段透传到 LightRAG QueryParam。
    # embedding_model / reranker_model 仍只记录日志（per-call profile 切换需要
    # 重建 LightRAG instance，Phase 3 才实现 — 当前 instance 由 route 静态决定）。
    if req.embedding_model or req.reranker_model:
        logger.info(
            'query received profile overrides (embedding=%r reranker=%r) — '
            'Phase 2 accepts but does not switch instance; effective in Phase 3.',
            req.embedding_model, req.reranker_model,
        )
    from lightrag import QueryParam
    # Cap concurrent queries — each runs LLM + embedding fan-out.
    async with _RAG_QUERY_SEM:
        lightrag = await get_lightrag(canonical)
        # QueryParam 字段按 LightRAG 默认值兜底；只在 req 显式给出时覆盖。
        # rerank=None 表示走 route 默认（由 LightRAG instance 的 enable_rerank=True
        # 默认 + rerank_model_func 是否为 None 共同决定）。
        qp_kwargs: dict[str, Any] = {'mode': req.mode, 'top_k': req.top_k}
        if req.rerank is not None:
            qp_kwargs['enable_rerank'] = bool(req.rerank)
        if req.chunk_top_k is not None:
            qp_kwargs['chunk_top_k'] = int(req.chunk_top_k)
        answer = await lightrag.aquery(req.query, param=QueryParam(**qp_kwargs))
    return QueryResponse(
        status='ok',
        query=req.query,
        container=req.container,
        answer=answer or '(no answer generated)',
        mode=req.mode,
    )


# ---------------------------------------------------------------------------
# Usage analytics admin endpoints (v0.17 — feeds dashboard Analytics view).
# Handlers are thin wrappers around the pure functions in usage_analytics; all
# query parameter validation lives there to keep this file readable.
# ---------------------------------------------------------------------------


@app.get(
    '/admin/usage/summary',
    response_model=UsageSummaryResponse,
    dependencies=[Depends(verify_auth)],
)
def admin_usage_summary(window: str = '24h') -> UsageSummaryResponse:
    data = usage_analytics.summary(_queue_db_path(), window=window)
    return UsageSummaryResponse(**data)


@app.get(
    '/admin/usage/endpoints',
    response_model=UsageEndpointsResponse,
    dependencies=[Depends(verify_auth)],
)
def admin_usage_endpoints(
    window: str = '7d',
    sort: str = 'calls',
    limit: int = 20,
) -> UsageEndpointsResponse:
    data = usage_analytics.endpoints(_queue_db_path(), window=window, sort=sort, limit=limit)
    return UsageEndpointsResponse(**data)


@app.get(
    '/admin/usage/containers',
    response_model=UsageContainersResponse,
    dependencies=[Depends(verify_auth)],
)
def admin_usage_containers(
    window: str = '7d',
    sort: str = 'calls',
    limit: int = 50,
) -> UsageContainersResponse:
    data = usage_analytics.containers(_queue_db_path(), window=window, sort=sort, limit=limit)
    return UsageContainersResponse(**data)


@app.get(
    '/admin/usage/timeseries',
    response_model=UsageTimeseriesResponse,
    dependencies=[Depends(verify_auth)],
)
def admin_usage_timeseries(
    path: str,
    window: str = '7d',
    bucket: str = '1h',
) -> UsageTimeseriesResponse:
    data = usage_analytics.timeseries(_queue_db_path(), path=path, window=window, bucket=bucket)
    return UsageTimeseriesResponse(**data)


@app.post(
    '/admin/usage/cleanup',
    response_model=UsageCleanupResponse,
    dependencies=[Depends(verify_auth)],
)
def admin_usage_cleanup(req: UsageCleanupRequest) -> UsageCleanupResponse:
    data = usage_analytics.cleanup(_queue_db_path(), retention_days=req.retention_days)
    return UsageCleanupResponse(**data)


# ---------------------------------------------------------------------------
# Admin dashboard (`/admin/ui/*`) — Lane D
#
# Cookie-based session login that proxies the existing api-key gate. Three
# JSON endpoints (login / logout / me) plus a SPA fallback that serves the
# built React bundle out of /app/static/admin when present.
#
# Cookie hardening: HttpOnly + SameSite=Strict always; Secure flag is enabled
# unless TM_ENV explicitly says "dev" (so the local docker-compose setup over
# plain HTTP still works while production stays strict). CSRF defence is the
# SameSite cookie plus a mandatory `X-Requested-With: XMLHttpRequest` header
# on POST routes — fetch() in the SPA sets it, a cross-site form submission
# cannot.
# ---------------------------------------------------------------------------

try:
    from task_rag_server_models import LoginRequest  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_server_models import LoginRequest  # noqa: E402


def _client_ip(request: Request) -> str:
    """Best-effort client IP behind an upstream proxy. Trusts the leftmost
    ``X-Forwarded-For`` entry only when an upstream proxy is presumably in
    front (compose default uses host networking + nginx)."""
    fwd = request.headers.get('x-forwarded-for')
    if fwd:
        return fwd.split(',')[0].strip()
    if request.client is not None:
        return request.client.host or ''
    return ''


def _cookie_secure() -> bool:
    """``Secure`` cookie flag — on by default, off only when TM_ENV=dev so
    plain-HTTP local debugging works. Never gate this on request.url.scheme
    alone — behind nginx the scheme upstream is always http."""
    return (os.environ.get('TM_ENV', 'prod') or 'prod').lower() not in ('dev', 'development', 'local')


def _require_csrf_header(request: Request) -> None:
    """POST routes refuse if the X-Requested-With header is missing. Combined
    with SameSite=Strict on the session cookie this blocks the obvious CSRF
    vectors (form post from another origin); browsers will not let JS code at
    another origin add this header without going through CORS preflight, which
    we don't allow."""
    if request.headers.get('x-requested-with', '').lower() != 'xmlhttprequest':
        raise HTTPException(status_code=400, detail='missing X-Requested-With header')


@app.post('/admin/ui/login')
async def admin_ui_login(req: LoginRequest, request: Request, response: Response) -> dict:
    """Verify the api key, mint a session, return the truncated hash + expiry.

    Lockout precedes the constant-time key check so timing oracles can't
    measure "was my key close" vs "am I rate-limited". A successful login also
    wipes the IP's failure log so a legit user who mistyped twice and then
    typed correctly is not penalised.
    """
    _require_csrf_header(request)
    if not RAG_API_KEY:
        raise HTTPException(status_code=500, detail='RAG_API_KEY not set')

    try:
        from auth_session import constant_time_equals, hash_api_key
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.auth_session import constant_time_equals, hash_api_key

    ip = _client_ip(request)
    ua = request.headers.get('user-agent', '')
    limit = get_ui_login_limit()
    if limit.is_locked(ip):
        limit.check_and_record(ip, success=False)
        raise HTTPException(status_code=429, detail='too many failed logins; try again later')

    ok = constant_time_equals(req.api_key or '', RAG_API_KEY)
    allowed = limit.check_and_record(ip, success=ok)
    if not allowed:
        raise HTTPException(status_code=429, detail='too many failed logins; try again later')
    if not ok:
        raise HTTPException(status_code=401, detail='invalid api key')

    info = get_ui_session_store().create(api_key=RAG_API_KEY, ip=ip, user_agent=ua)
    response.set_cookie(
        key='tm_sid',
        value=info.token,
        max_age=max(60, info.expires_at - int(time.time())),
        httponly=True,
        secure=_cookie_secure(),
        samesite='strict',
        path='/',
    )
    return {
        'api_key_hash': hash_api_key(RAG_API_KEY)[:12],
        'expires_at': info.expires_at,
        'env': os.environ.get('TM_ENV', 'dev'),
    }


@app.post('/admin/ui/logout')
async def admin_ui_logout(request: Request, response: Response) -> dict:
    """Revoke the current cookie (if any) and clear the browser-side cookie."""
    _require_csrf_header(request)
    token = request.cookies.get('tm_sid')
    if token:
        get_ui_session_store().revoke(token)
    response.delete_cookie('tm_sid', path='/')
    return {'status': 'ok'}


@app.get('/admin/ui/me')
async def admin_ui_me(request: Request) -> dict:
    """Cheap session probe used by the SPA's ``useMe()`` hook on every nav.

    Returns 401 if the cookie is missing or invalid; the React router uses that
    to bounce the browser to ``/admin/ui/login``. We don't fall back to the
    header path here — the dashboard is cookie-only by design.
    """
    try:
        from auth_session import hash_api_key
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.auth_session import hash_api_key

    token = request.cookies.get('tm_sid')
    info = get_ui_session_store().validate(token)
    if info is None:
        raise HTTPException(status_code=401, detail='no session')
    return {
        'api_key_hash': hash_api_key(RAG_API_KEY)[:12] if RAG_API_KEY else '',
        'expires_at': info.expires_at,
        'env': os.environ.get('TM_ENV', 'dev'),
    }


# StaticFiles mount for the built SPA. The Dockerfile copies the Vite output
# to /app/static/admin; for local dev (running uvicorn from a checkout) the
# same relative path resolves under the repo root if a build was produced.
# When neither exists we skip the mount — `/admin/ui/login` etc. still answer
# JSON so the API contract is preserved even without the front-end assets.
_UI_STATIC_DIR_CANDIDATES = [
    Path('/app/static/admin'),
    Path(__file__).resolve().parent.parent / 'static' / 'admin',
    Path(__file__).resolve().parent.parent / 'dashboard' / 'dist',
]


def _resolve_ui_static_dir() -> Path | None:
    for candidate in _UI_STATIC_DIR_CANDIDATES:
        if candidate.is_dir() and (candidate / 'index.html').exists():
            return candidate
    return None


_UI_STATIC_DIR = _resolve_ui_static_dir()
if _UI_STATIC_DIR is not None:
    if (_UI_STATIC_DIR / 'assets').is_dir():
        app.mount(
            '/admin/ui/assets',
            StaticFiles(directory=str(_UI_STATIC_DIR / 'assets')),
            name='admin_ui_assets',
        )

    @app.get('/admin/ui')
    async def admin_ui_root_noslash() -> FileResponse:
        return FileResponse(str(_UI_STATIC_DIR / 'index.html'))

    @app.get('/admin/ui/')
    async def admin_ui_root() -> FileResponse:
        return FileResponse(str(_UI_STATIC_DIR / 'index.html'))

    @app.get('/admin/ui/{full_path:path}')
    async def admin_ui_spa_fallback(full_path: str) -> FileResponse:
        """SPA deep-link fallback. ``/admin/ui/containers/foo`` is owned by
        React Router; if the path matches a real file under the static dir
        we serve it, otherwise we return index.html and let the client-side
        router take it from there.

        The explicit JSON endpoints (`login` / `logout` / `me`) are registered
        before this catch-all, so FastAPI's router resolves them first."""
        candidate = (_UI_STATIC_DIR / full_path).resolve()
        try:
            candidate.relative_to(_UI_STATIC_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail='not found')
        if candidate.is_file():
            return FileResponse(str(candidate))
        index_path = _UI_STATIC_DIR / 'index.html'
        if not index_path.exists():
            raise HTTPException(status_code=404, detail='dashboard bundle missing')
        return FileResponse(str(index_path))
