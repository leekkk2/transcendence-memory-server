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

from fastapi import Depends, FastAPI, Header, HTTPException

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
    from arch_detect import detect_architecture, reset_cache as reset_arch_cache
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.arch_detect import detect_architecture, reset_cache as reset_arch_cache


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup_banner()
    yield


app = FastAPI(lifespan=lifespan)


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


def run_or_start(cmd: list[str], timeout_s: int, background: bool | None, wait: bool) -> CommandResponse:
    if not Path(cmd[0]).exists():
        return CommandResponse(command=cmd, code=127, stderr=f'script not found: {cmd[0]}')
    real_cmd = [sys.executable, *cmd] if cmd[0].endswith('.py') else cmd
    run_in_background = background if background is not None else not wait
    if run_in_background:
        process = subprocess.Popen(real_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env())
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


@app.post('/embed', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def embed(req: ContainerReq) -> CommandResponse:
    return run_or_start([str(script_path('task_rag_lancedb_ingest.py')), '--container', req.container], req.timeout_s, req.background, req.wait)


@app.post('/build-manifest', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def build_manifest(_req: ContainerReq) -> CommandResponse:
    return CommandResponse(command=[], code=0, status='deprecated', note='build-manifest was removed in LanceDB-only mode; use /embed.')


@app.post('/ingest-memory', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_memory(req: IngestMemoryReq) -> CommandResponse:
    command = [str(script_path('task_rag_lancedb_ingest.py')), '--container', req.container]
    if req.memory_dir:
        command += ['--memory-dir', req.memory_dir]
    if req.archive_dir:
        command += ['--archive-dir', req.archive_dir]
    return run_or_start(command, req.timeout_s, req.background, req.wait)


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
    path = memory_objects_path(req.container)
    lines = []
    for obj in req.objects:
        payload = obj.model_dump(mode='json')
        payload['storedAt'] = int(time.time())
        lines.append(json.dumps(payload, ensure_ascii=False))
    with path.open('a', encoding='utf-8') as handle:
        for line in lines:
            handle.write(line + '\n')

    # auto_embed: 自动在后台触发索引重建
    if req.auto_embed:
        embed_script = script_path('task_rag_lancedb_ingest.py')
        if embed_script.exists():
            run_or_start(
                [str(embed_script), '--container', req.container],
                timeout_s=600,
                background=True,
                wait=False,
            )

    index_hint = 'Auto-embed triggered in background.' if req.auto_embed else 'Run /embed for this container to refresh LanceDB after storing new objects.'
    return ClientIngestResponse(
        container=req.container,
        accepted=len(lines),
        stored_path=str(path),
        stored_paths=[str(path)],
        index_hint=index_hint,
    )


@app.post('/ingest-structured', response_model=CommandResponse, dependencies=[Depends(verify_auth)])
def ingest_structured(req: StructuredIngestReq) -> CommandResponse:
    command = [
        str(script_path('task_rag_structured_ingest.py')),
        '--container', req.container,
        '--input', req.input_path,
        '--doc-type', req.doc_type,
    ]
    if req.doc_id:
        command += ['--doc-id', req.doc_id]
    return run_or_start(command, req.timeout_s, req.background, req.wait)


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


def read_memory_objects(container: str) -> list[dict]:
    """读取 container 下的 memory_objects.jsonl，返回 dict 列表。"""
    path = memory_objects_path(container)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def write_memory_objects(container: str, rows: list[dict]) -> Path:
    """原子写入 memory_objects.jsonl（tmp + rename）。"""
    path = memory_objects_path(container)
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


@app.get('/jobs/{pid}', response_model=JobStatusResponse, dependencies=[Depends(verify_auth)])
def job_status(pid: int) -> JobStatusResponse:
    try:
        os.kill(pid, 0)
        return JobStatusResponse(pid=pid, running=True, message=f'Process {pid} is running.')
    except ProcessLookupError:
        return JobStatusResponse(pid=pid, running=False, exit_code=None, message=f'Process {pid} not found.')
    except PermissionError:
        return JobStatusResponse(pid=pid, running=True, message=f'Process {pid} exists (permission denied for signal).')


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
    lightrag = await get_lightrag(req.container)
    await lightrag.ainsert(req.text)
    return QueryResponse(
        status='ok',
        query='',
        container=req.container,
        answer=f'Text ingested into container {req.container} knowledge graph.',
        mode='insert',
    )


@app.post('/query', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def query_rag(req: QueryReq) -> QueryResponse:
    validate_container_name(req.container)
    _require_lightrag_ready()
    from lightrag import QueryParam
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
