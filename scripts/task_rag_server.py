#!/usr/bin/env python3
"""Canonical FastAPI server for transcendence-memory-server."""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException

try:
    from task_rag_server_models import (
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
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
        ClientIngestReq,
        ClientIngestResponse,
        CommandResponse,
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
        QueryReq,
        QueryResponse,
        SearchHit,
        SearchReq,
        SearchResponse,
        StructuredIngestReq,
        UpdateMemoryReq,
    )

try:
    from rag_engine import get_rag, ensure_rag_initialized
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.rag_engine import get_rag, ensure_rag_initialized


WS = Path(os.environ.get('WORKSPACE', '/home/ubuntu/.openclaw/workspace'))
SERVER_SCRIPTS = Path(__file__).resolve().parent
WORKSPACE_SCRIPTS = WS / 'scripts'
RAG_API_KEY = os.environ.get('RAG_API_KEY', '')
SERVER_STARTED_AT = time.time()


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


app = FastAPI()


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    if env.get('EMBEDDING_BASE_URL') and not env.get('EMBEDDINGS_BASE_URL'):
        env['EMBEDDINGS_BASE_URL'] = env['EMBEDDING_BASE_URL']
    return env


def run(cmd: list[str], timeout_s: int) -> CommandResponse:
    script = Path(cmd[0])
    if not script.exists():
        return CommandResponse(command=cmd, code=127, stderr=f'script not found: {script}')
    real_cmd = [sys.executable, *cmd] if cmd[0].endswith('.py') else cmd
    try:
        completed = subprocess.run(real_cmd, capture_output=True, text=True, timeout=timeout_s, env=child_env())
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
def health() -> HealthResponse:
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
    return HealthResponse(
        status='ok',
        service='transcendence-memory-server',
        architecture='lancedb-only',
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
        },
        available_containers=sorted(path.name for path in containers.iterdir() if path.is_dir()) if containers.exists() else [],
        warnings=warnings,
        uptime_seconds=max(0, int(time.time() - SERVER_STARTED_AT)),
    )


@app.post('/search', response_model=SearchResponse, dependencies=[Depends(verify_auth)])
def search(req: SearchReq) -> SearchResponse:
    result = run([str(script_path('task_rag_search.py')), '--query', req.query, '--topk', str(req.topk), '--container', req.container], req.timeout_s)
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    raw_results = payload.get('results') if isinstance(payload, dict) else []
    if not isinstance(raw_results, list):
        raw_results = []
    parsed = [SearchHit(**item) for item in raw_results if isinstance(item, dict)]
    return SearchResponse(
        status='ok' if result.code == 0 else 'error',
        command=result.command,
        code=result.code,
        query=req.query,
        topk=req.topk,
        container=req.container,
        initialized=bool(payload.get('initialized')) if isinstance(payload, dict) else False,
        message=str(payload.get('message')) if isinstance(payload, dict) and payload.get('message') else None,
        results=parsed,
        stdout=result.stdout,
        stderr=result.stderr,
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
    return {
        'mode': 'lancedb-only',
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
    return ClientIngestResponse(
        container=req.container,
        accepted=len(lines),
        stored_path=str(path),
        stored_paths=[str(path)],
        index_hint='Run /embed for this container to refresh LanceDB after storing new objects.',
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
    tmp_path.rename(path)
    return path


# --- 新端点 ---


@app.get('/export-connection-token', response_model=ConnectionTokenResponse, dependencies=[Depends(verify_auth)])
def export_connection_token(container: str = DEFAULT_CONTAINER) -> ConnectionTokenResponse:
    endpoint = os.environ.get('RAG_ADVERTISED_ENDPOINT', 'http://localhost:8711')
    payload = json.dumps({'endpoint': endpoint, 'api_key': RAG_API_KEY, 'container': container}, ensure_ascii=False)
    token = base64.b64encode(payload.encode('utf-8')).decode('ascii')
    return ConnectionTokenResponse(
        token=token,
        endpoint=endpoint,
        container=container,
        note='Base64-encoded connection token. Decode to get endpoint, api_key, container.',
    )


@app.get('/containers', response_model=ContainerListResponse, dependencies=[Depends(verify_auth)])
def list_containers() -> ContainerListResponse:
    containers_dir = WS / 'tasks' / 'rag' / 'containers'
    if not containers_dir.exists():
        return ContainerListResponse(containers=[], count=0)
    names = sorted(p.name for p in containers_dir.iterdir() if p.is_dir())
    return ContainerListResponse(containers=names, count=len(names))


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


@app.post('/documents/text', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def ingest_document_text(req: DocumentTextReq) -> QueryResponse:
    validate_container_name(req.container)
    try:
        rag = await ensure_rag_initialized(req.container)
        await rag.lightrag.ainsert(req.text)
        return QueryResponse(
            status='ok',
            query='',
            container=req.container,
            answer=f'Text ingested into container {req.container} knowledge graph.',
            mode='insert',
        )
    except Exception as exc:
        return QueryResponse(
            status='error',
            query='',
            container=req.container,
            answer=f'Ingest failed: {exc}',
            mode='insert',
        )


@app.post('/query', response_model=QueryResponse, dependencies=[Depends(verify_auth)])
async def query_rag(req: QueryReq) -> QueryResponse:
    validate_container_name(req.container)
    try:
        rag = await ensure_rag_initialized(req.container)
        from lightrag import QueryParam
        answer = await rag.lightrag.aquery(
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
    except Exception as exc:
        return QueryResponse(
            status='error',
            query=req.query,
            container=req.container,
            answer=f'Query failed: {exc}',
            mode=req.mode,
        )
