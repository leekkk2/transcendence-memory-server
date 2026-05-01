#!/usr/bin/env python3
"""Canonical FastAPI server for transcendence-memory-server."""
from __future__ import annotations

import base64
import fnmatch
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from contextlib import asynccontextmanager

import tempfile
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile

try:
    from task_rag_server_models import (
        AgentOnboardingResponse,
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
        ConfigurationGuide,
        ConnectionTokenResponse,
        ContainerDeleteResponse,
        ContainerListResponse,
        ContainerReq,
        DEFAULT_CONTAINER,
        DocumentTextReq,
        HealthResponse,
        IngestMemoryReq,
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
    )
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_server_models import (
        AgentOnboardingResponse,
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
        ConfigurationGuide,
        ConnectionTokenResponse,
        ContainerDeleteResponse,
        ContainerListResponse,
        ContainerReq,
        DEFAULT_CONTAINER,
        DocumentTextReq,
        HealthResponse,
        IngestMemoryReq,
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


# Async semaphores for unbounded-cost RAG paths (lightrag write/query, mineru
# upload). One semaphore per category so they don't starve each other.
import asyncio as _asyncio

_RAG_WRITE_SEM = _asyncio.Semaphore(_env_int('TM_MAX_CONCURRENT_RAG_WRITES', 1))
_RAG_QUERY_SEM = _asyncio.Semaphore(_env_int('TM_MAX_CONCURRENT_RAG_QUERIES', 2))
_DOC_FILE_SEM = _asyncio.Semaphore(_env_int('TM_MAX_CONCURRENT_DOC_FILES', 1))
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


def verify_auth(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> None:
    if not RAG_API_KEY:
        raise HTTPException(status_code=500, detail='RAG_API_KEY not set')

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global JOB_WORKER
    _startup_banner()
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
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=413,
                    content={'detail': f'file exceeds max upload size {_MAX_UPLOAD_BYTES_ENV} bytes'},
                )
    return await call_next(request)


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    if env.get('EMBEDDING_BASE_URL') and not env.get('EMBEDDINGS_BASE_URL'):
        env['EMBEDDINGS_BASE_URL'] = env['EMBEDDING_BASE_URL']
    # Force UTF-8 for subprocess JSON I/O so Windows pipes can safely carry non-ASCII text.
    env.setdefault('PYTHONUTF8', '1')
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    return env


def run(cmd: list[str], timeout_s: int) -> CommandResponse:
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
            env=child_env(),
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
) -> CommandResponse:
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
        process = subprocess.Popen(real_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env())
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
    result = run(cmd, timeout_s=timeout_s)
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


@app.get('/health', response_model=HealthResponse)
async def health(container: str | None = None) -> HealthResponse:
    containers = WS / 'tasks' / 'rag' / 'containers'
    scripts_present = {
        'search': script_path('task_rag_search.py').exists(),
        'lancedb_ingest': script_path('task_rag_lancedb_ingest.py').exists(),
        'structured_ingest': script_path('task_rag_structured_ingest.py').exists(),
    }
    embedding_configured = bool(os.environ.get('EMBEDDING_API_KEY'))
    lancedb_available = importlib.util.find_spec('lancedb') is not None
    warnings: list[str] = []
    if not RAG_API_KEY:
        warnings.append('RAG_API_KEY is not configured.')
    if not embedding_configured:
        warnings.append('EMBEDDING_API_KEY is not configured.')
    if not lancedb_available:
        warnings.append('lancedb runtime is unavailable.')
    if not containers.exists():
        warnings.append('containers root does not exist yet; it will be created on first ingest.')

    # 架构检测
    arch = detect_architecture()
    warnings.extend(arch.degraded_reasons)

    # 模块状态
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
            warnings.append(f'LightRAG probe failed for container={container}: {exc}')

    # 系统健康快照：客户端可据此提前退避，避免请求堆叠
    sys_snap = read_system_health()
    admit_ok, admit_reason = GATE.check_admit(sys_snap)
    if not admit_ok:
        warnings.append(f'system pressure: {admit_reason}')

    # Queue stats: surface backlog so clients can decide whether to keep posting.
    try:
        queue_stats = get_job_queue().stats()
    except Exception as exc:  # pragma: no cover - defensive
        queue_stats = {'error': str(exc)}
        warnings.append(f'job queue inaccessible: {exc}')
    worker_running = bool(JOB_WORKER and JOB_WORKER.is_running)
    if not worker_running and not DISABLE_WORKER:
        warnings.append('background ingest worker is not running')

    return HealthResponse(
        status='ok',
        service='transcendence-memory-server',
        architecture=arch.name,
        build_flavor=arch.build_flavor,
        multimodal_capable=arch.multimodal_capable,
        degraded_reasons=arch.degraded_reasons,
        workspace=str(WS),
        containers_root=str(containers),
        auth_configured=bool(RAG_API_KEY),
        embedding_configured=embedding_configured,
        lancedb_available=lancedb_available,
        scripts_present=scripts_present,
        runtime_ready={
            'search': scripts_present['search'] and embedding_configured and lancedb_available,
            'embed': scripts_present['lancedb_ingest'] and embedding_configured and lancedb_available,
            'ingest_memory': scripts_present['lancedb_ingest'] and embedding_configured and lancedb_available,
            'ingest_objects': True,
            'ingest_structured': scripts_present['structured_ingest'] and embedding_configured and lancedb_available,
            'query': documents_text_ready,
            'documents_text': documents_text_ready,
        },
        available_containers=sorted(path.name for path in containers.iterdir() if path.is_dir()) if containers.exists() else [],
        warnings=warnings,
        uptime_seconds=max(0, int(time.time() - SERVER_STARTED_AT)),
        modules=modules_resp,
        configuration_guide=config_guide,
        system=sys_snap.as_dict(),
        accepting_ingest=admit_ok,
        background_jobs_active=BG_TRACKER.count_active(),
        queue_stats=queue_stats,
        worker_running=worker_running,
    )


def _resolve_search_targets(req: SearchReq) -> list[str]:
    """根据 SearchReq 解析最终要查询的容器列表。

    优先级：containers > container_pattern > container。
    """
    if req.containers:
        for name in req.containers:
            validate_container_name(name)
        # 去重保序
        seen: set[str] = set()
        ordered: list[str] = []
        for name in req.containers:
            if name not in seen:
                ordered.append(name)
                seen.add(name)
        return ordered

    if req.container_pattern is not None:
        _validate_pattern(req.container_pattern)
        names = [
            p.name for p in _list_container_dirs()
            if _match_container(p.name, req.container_pattern, req.pattern_mode)
        ]
        return names

    validate_container_name(req.container)
    return [req.container]


def _run_single_search(query: str, topk: int, container: str, timeout_s: int) -> tuple[CommandResponse, dict]:
    result = run(
        [str(script_path('task_rag_search.py')), '--query', query, '--topk', str(topk), '--container', container],
        timeout_s,
    )
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return result, payload


@app.post('/search', response_model=SearchResponse, dependencies=[Depends(verify_auth)])
def search(req: SearchReq) -> SearchResponse:
    targets = _resolve_search_targets(req)

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
        )

    # 跨容器召回时拉宽每容器 topk，避免被某个容器全占
    per_container_topk = req.topk if len(targets) == 1 else min(req.topk * len(targets), 100)

    per_status: dict[str, str] = {}
    all_hits: list[SearchHit] = []
    last_command: list[str] = []
    last_stdout = ''
    last_stderr = ''
    any_initialized = False

    def _do(name: str) -> tuple[str, CommandResponse, dict]:
        cmd_result, payload = _run_single_search(req.query, per_container_topk, name, req.timeout_s)
        return name, cmd_result, payload

    if len(targets) == 1:
        results_iter = [_do(targets[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as pool:
            futures = [pool.submit(_do, name) for name in targets]
            results_iter = [f.result() for f in as_completed(futures)]

    for name, cmd_result, payload in results_iter:
        last_command = cmd_result.command
        last_stdout = cmd_result.stdout
        last_stderr = cmd_result.stderr
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

    # LanceDB 距离越小越相关；None 视为最差
    all_hits.sort(key=lambda hit: (hit.score is None, hit.score if hit.score is not None else 0.0))
    merged = all_hits[: req.topk]

    has_any_ok = any(s == 'ok' for s in per_status.values())
    primary_container = targets[0] if len(targets) == 1 else (req.container if req.container in targets else targets[0])

    message = None
    if not has_any_ok and per_status:
        message = '; '.join(f'{name}: {status}' for name, status in per_status.items())

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
    raise ValueError(f'unknown op: {op}')


def _enqueue_or_run(
    op: str,
    container: str,
    payload: dict,
    timeout_s: int,
    wait: bool,
    label: str = '',
) -> CommandResponse:
    """Dispatch one of the three ingest ops, choosing between three modes:

    1. wait=False (default): enqueue into persistent SQLite queue, return job_id.
       The single background worker drains the queue at a steady, host-friendly
       pace. Coalescing prevents duplicate enqueues for (op, container).

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
            return run(cmd, timeout_s=timeout_s)
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
    return _enqueue_or_run(
        op='embed',
        container=req.container,
        payload={},
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='embed',
    )


@app.post('/build-manifest', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def build_manifest(_req: ContainerReq) -> CommandResponse:
    return CommandResponse(command=[], code=0, status='deprecated', note='build-manifest was removed in LanceDB-only mode; use /embed.')


@app.post('/ingest-memory', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_memory(req: IngestMemoryReq) -> CommandResponse:
    payload = {}
    if req.memory_dir:
        payload['memory_dir'] = req.memory_dir
    if req.archive_dir:
        payload['archive_dir'] = req.archive_dir
    return _enqueue_or_run(
        op='ingest-memory',
        container=req.container,
        payload=payload,
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='ingest-memory',
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
    path = memory_objects_path(req.container)
    lines = []
    for obj in req.objects:
        payload = obj.model_dump(mode='json')
        payload['storedAt'] = int(time.time())
        lines.append(json.dumps(payload, ensure_ascii=False))
    # POSIX O_APPEND is atomic only for writes < PIPE_BUF (~4 KB). A single
    # large object can tear if two requests race; a per-container lock
    # serializes appends on the same JSONL file.
    container_lock = GATE._container_lock(req.container)  # noqa: SLF001 — intentional reuse
    with container_lock:
        with path.open('a', encoding='utf-8') as handle:
            for line in lines:
                handle.write(line + '\n')

    # auto_embed: enqueue a background embed job. The persistent queue coalesces
    # duplicate enqueues for the same container, so even posting many objects
    # in a tight loop only yields one pending embed job. The queue worker will
    # drain it later at a stable, host-friendly pace.
    if req.auto_embed:
        try:
            get_job_queue().enqueue(
                op='embed', container=req.container, payload={}, label='auto-embed',
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
    return ClientIngestResponse(
        container=req.container,
        accepted=len(lines),
        stored_path=str(path),
        stored_paths=[str(path)],
        index_hint=index_hint,
    )


@app.post('/ingest-structured', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_structured(req: StructuredIngestReq) -> CommandResponse:
    payload = {
        'input_path': req.input_path,
        'doc_type': req.doc_type,
    }
    if req.doc_id:
        payload['doc_id'] = req.doc_id
    return _enqueue_or_run(
        op='ingest-structured',
        container=req.container,
        payload=payload,
        timeout_s=req.timeout_s,
        wait=req.wait,
        label='ingest-structured',
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
    """
    if mode not in ('substring', 'prefix', 'glob'):
        raise HTTPException(status_code=400, detail=f'invalid mode: {mode}')
    if pattern is not None:
        _validate_pattern(pattern)

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
        result.append({
            'name': p.name,
            'objects': obj_count,
            'indexed': indexed,
            'last_modified': last_mod,
        })
    return {'containers': result, 'count': len(result)}


@app.delete('/containers/{name}', response_model=ContainerDeleteResponse, dependencies=[Depends(verify_auth)])
def delete_container(name: str) -> ContainerDeleteResponse:
    validate_container_name(name)
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
    rows = read_memory_objects(container)
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
    write_memory_objects(container, rows)
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
    rows = read_memory_objects(container)
    new_rows = [r for r in rows if r.get('id') != memory_id]
    if len(new_rows) == len(rows):
        raise HTTPException(status_code=404, detail=f'memory object not found: {memory_id}')
    write_memory_objects(container, new_rows)
    return MemoryDeleteResponse(
        container=container,
        id=memory_id,
        deleted=True,
        message='Memory object deleted.',
    )


@app.get('/admin/system-health', dependencies=[Depends(verify_auth)])
def admin_system_health() -> dict:
    """运维诊断端点：返回详细的保护层状态。

    与 /health 不同：
    - /health 暴露最少必要的系统快照供客户端退避决策；
    - /admin/system-health 提供 background job 明细，方便排查"为什么 ingest 被拒"。
    """
    snap = read_system_health()
    admit_ok, admit_reason = GATE.check_admit(snap)
    return {
        'system': snap.as_dict(),
        'admit_ok': admit_ok,
        'admit_reason': admit_reason,
        'gate_config': {
            'max_concurrent': GATE.config.max_concurrent,
            'min_available_mem_mb': GATE.config.min_available_mem_mb,
            'max_load_per_cpu': GATE.config.max_load_per_cpu,
            'max_swap_used_pct': GATE.config.max_swap_used_pct,
        },
        'background_jobs': BG_TRACKER.list_active(),
        'background_max_alive': BG_TRACKER.max_alive,
        'retry_cooldown_sec': RETRY_LIMITER.cooldown_sec,
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


@app.post('/documents/text', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def ingest_document_text(req: DocumentTextReq) -> QueryResponse:
    validate_container_name(req.container)
    _require_lightrag_ready()
    _admit_or_503(req.container, op='documents/text')
    # Cap concurrent lightrag writes — each call drives many embedding+LLM
    # invocations and holds the container's KV graph lock.
    async with _RAG_WRITE_SEM:
        lightrag = await get_lightrag(req.container)
        await lightrag.ainsert(req.text)
    return QueryResponse(
        status='ok',
        query='',
        container=req.container,
        answer=f'Text ingested into container {req.container} knowledge graph.',
        mode='insert',
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


@app.post('/documents/file', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def ingest_document_file(
    container: str = Form(...),
    file: UploadFile = File(...),
    parse_method: str | None = Form(default=None),
) -> QueryResponse:
    """多模态文档入库：PDF / Office / 图片 / HTML / Markdown 等。

    底层走 RAGAnything.process_document_complete → mineru parser → LightRAG。
    与 /documents/text 共享同一个 container working_dir，写入同一知识图谱。

    Mineru parsing routinely uses 1-2 GB RAM; we admit-gate + serialize via
    _DOC_FILE_SEM (default concurrency=1) so two simultaneous uploads can't
    OOM a 1.5 GB container.
    """
    validate_container_name(container)
    _require_lightrag_ready()
    if get_raganything is None:
        raise HTTPException(status_code=503, detail='raganything package not installed; rebuild with multimodal flavor.')

    filename = _sanitize_upload_filename(file.filename)
    _admit_or_503(container, op='documents/file')

    # Stage uploads under /data/scratch (persistent volume, ~no size cap)
    # rather than /tmp tmpfs (128 MB), since MAX_UPLOAD_BYTES defaults to
    # 200 MB and mineru's intermediate parse output can be hundreds of MB more.
    scratch_root = WS / 'scratch' / 'uploads'
    scratch_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix='tm-upload-', dir=str(scratch_root)))
    saved = tmp_dir / filename
    # 防御性：验证 resolve 后仍在 tmp_dir 内
    if not str(saved.resolve()).startswith(str(tmp_dir.resolve()) + os.sep):
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail='invalid filename: path traversal')

    total = 0
    try:
        async with _DOC_FILE_SEM:
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

            rag = await get_raganything(container)
            parse_output = tmp_dir / 'parsed'
            parse_output.mkdir(exist_ok=True)
            parser_kwargs: dict[str, Any] = {}
            backend = os.environ.get('RAG_PARSER_BACKEND')
            if backend:
                parser_kwargs['backend'] = backend
            lang = os.environ.get('RAG_PARSER_LANG')
            if lang:
                parser_kwargs['lang'] = lang
            await rag.process_document_complete(
                file_path=str(saved),
                output_dir=str(parse_output),
                parse_method=parse_method or os.environ.get('RAG_PARSE_METHOD', 'auto'),
                **parser_kwargs,
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return QueryResponse(
        status='ok',
        query='',
        container=container,
        answer=f'Document {filename} ({total} bytes) ingested into container {container}.',
        mode='insert',
    )


@app.post('/query', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def query_rag(req: QueryReq) -> QueryResponse:
    validate_container_name(req.container)
    _require_lightrag_ready()
    _admit_or_503(req.container, op='query')
    from lightrag import QueryParam
    # Cap concurrent queries — each runs LLM + embedding fan-out.
    async with _RAG_QUERY_SEM:
        lightrag = await get_lightrag(req.container)
        answer = await lightrag.aquery(
            req.query,
            param=QueryParam(mode=req.mode, top_k=req.top_k),
        )
    return QueryResponse(
        status='ok',
        query=req.query,
        container=req.container,
        answer=answer or '(no answer generated)',
        mode=req.mode,
    )
