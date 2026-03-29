#!/usr/bin/env python3
"""Lightweight RAG server for task retrieval on Eva.

Endpoints:
- POST /search {query, topk?, container?}
- POST /embed {container}
- POST /build-manifest {container}
- POST /ingest-memory {container, memory_dir?, archive_dir?}

Run:
  uvicorn task_rag_server:app --host 0.0.0.0 --port 8711
"""
from __future__ import annotations
from fastapi import FastAPI, Header, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import Literal
import subprocess
from pathlib import Path
import sys
import tempfile
import json
import time

import os

WS = Path(os.environ.get('WORKSPACE', '/home/ubuntu/.openclaw/workspace'))
SCRIPTS = WS / 'scripts'

RAG_API_KEY = os.environ.get('RAG_API_KEY', '')
SERVER_STARTED_AT = time.time()


def verify_auth(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
    if not RAG_API_KEY:
        raise HTTPException(status_code=500, detail='RAG_API_KEY not set')
    key = None
    if authorization and authorization.lower().startswith('bearer '):
        key = authorization.split(' ', 1)[1]
    elif x_api_key:
        key = x_api_key
    if key != RAG_API_KEY:
        raise HTTPException(status_code=401, detail='unauthorized')


app = FastAPI(dependencies=[Depends(verify_auth)])


class SearchReq(BaseModel):
    query: str = Field(..., description='Natural-language query text.')
    topk: int = Field(default=5, ge=1, le=100, description='Maximum number of hits to return.')
    container: str = Field(default='imac', min_length=1, description='Server-side container to search in.')
    timeout_s: int = Field(default=120, ge=1, le=600, description='Optional timeout in seconds for server-side script execution.')


class ContainerReq(BaseModel):
    container: str = Field(default='imac', min_length=1, description='Server-side container name.')
    timeout_s: int = Field(default=120, ge=1, le=600, description='Optional timeout in seconds for server-side script execution.')


class IngestReq(BaseModel):
    container: str = Field(default='imac', min_length=1, description='Server-side container name.')
    timeout_s: int = Field(default=120, ge=1, le=600, description='Optional timeout in seconds for server-side script execution.')
    memory_dir: str | None = Field(default=None, description='Optional override path for memory source.')
    archive_dir: str | None = Field(default=None, description='Optional override path for archive source.')


class EmbedRequest(ContainerReq):
    pass


class BuildManifestRequest(ContainerReq):
    pass


class IngestMemoryRequest(IngestReq):
    pass


class IngestObject(BaseModel):
    id: str = Field(..., min_length=1, description='Client-provided stable object identifier.')
    text: str = Field(..., min_length=1, description='Primary retrievable text payload.')
    title: str | None = Field(default=None, description='Optional short title or summary.')
    source: str | None = Field(default=None, description='Optional client/source identifier.')
    tags: list[str] = Field(default_factory=list, description='Optional searchable tags.')
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict, description='Optional flat metadata payload.')


class ClientIngestReq(BaseModel):
    container: str = Field(default='imac', min_length=1, description='Server-side container name.')
    objects: list[IngestObject] = Field(..., min_length=1, description='Client-provided memory objects to persist for later indexing.')


class HealthResponse(BaseModel):
    status: Literal['ok']
    service: str
    workspace: str
    workspace_exists: bool
    workspace_writable: bool
    containers_root: str
    containers_root_exists: bool
    containers_root_writable: bool
    scripts_dir: str
    scripts_dir_exists: bool
    scripts_dir_writable: bool
    python: str
    auth_configured: bool
    scripts_present: dict[str, bool]
    embedding_configured: bool
    embedding_provider_base_url: str
    faiss_available: bool
    runtime_ready: dict[str, bool]
    storage_ready: bool
    warnings: list[str]
    default_container: str
    available_containers: list[str]
    endpoint_defaults: dict[str, str]
    uptime_seconds: int
    server_time_epoch_s: int
    endpoint_contracts: dict[str, str]
    server_architecture: dict[str, str | bool]


class CommandResult(BaseModel):
    command: list[str]
    code: int
    stdout: str
    stderr: str


class EmbedResponse(CommandResult):
    pass


class BuildManifestResponse(CommandResult):
    pass


class IngestMemoryResponse(CommandResult):
    pass


class SearchHit(BaseModel):
    path: str | None = None
    score: float | None = None
    text: str | None = None
    taskId: str | None = None
    chunkId: str | None = None
    docType: str | None = None
    sourcePath: str | None = None
    section: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class SearchResponse(BaseModel):
    status: Literal['ok', 'error']
    command: list[str]
    code: int
    query: str
    topk: int
    container: str
    results: list[SearchHit]
    stdout: str
    stderr: str


class IngestContractResponse(BaseModel):
    mode: Literal['server-side-container-ingest']
    content_source: Literal['client-provided']
    storage_location: str
    retrieval_scope: str
    notes: list[str]


class ClientIngestResponse(BaseModel):
    container: str
    accepted: int
    stored_path: str
    stored_paths: list[str]
    index_hint: str


SearchResponse.model_rebuild()


def run(cmd: list[str], timeout_s: int = 120) -> CommandResult:
    script = Path(cmd[0])
    if not script.exists():
        return CommandResult(command=cmd, code=127, stdout='', stderr=f'script not found: {script}')
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        return CommandResult(command=cmd, code=p.returncode, stdout=p.stdout, stderr=p.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if stderr:
            stderr = f"{stderr}\n"
        return CommandResult(command=cmd, code=124, stdout=stdout, stderr=f"{stderr}timeout after {timeout_s}s")
    except Exception as exc:
        return CommandResult(command=cmd, code=1, stdout='', stderr=f'command failed: {exc}')

@app.get('/health', response_model=HealthResponse)
def health() -> HealthResponse:
    expected_scripts = {
        'search': SCRIPTS / 'task_rag_search.py',
        'embed': SCRIPTS / 'task_rag_embed.py',
        'build_manifest': SCRIPTS / 'task_rag_build_manifest.py',
        'ingest_memory': SCRIPTS / 'task_rag_ingest_memory_refs.py',
    }
    scripts_present = {name: path.exists() for name, path in expected_scripts.items()}
    scripts_dir_exists = SCRIPTS.exists()
    scripts_dir_writable = os.access(SCRIPTS, os.W_OK) if scripts_dir_exists else False
    embedding_api_key = os.environ.get('EMBEDDING_API_KEY', '')
    embeddings_base_url = os.environ.get('EMBEDDINGS_BASE_URL', '')
    google_embedding_base_url = os.environ.get('GOOGLE_EMBEDDING_BASE_URL', '')
    embedding_provider_base_url = embeddings_base_url or google_embedding_base_url

    try:
        import faiss  # type: ignore  # noqa: F401
        faiss_available = True
    except Exception:
        faiss_available = False

    workspace_exists = WS.exists()
    workspace_writable = os.access(WS, os.W_OK)
    containers_root = WS / 'tasks' / 'rag' / 'containers'
    containers_root_exists = containers_root.exists()
    containers_root_writable = os.access(containers_root, os.W_OK) if containers_root_exists else False
    default_container = 'imac'
    available_containers = sorted(
        path.name for path in containers_root.iterdir() if path.is_dir()
    ) if containers_root_exists else []
    warnings: list[str] = []

    if not RAG_API_KEY:
        warnings.append('RAG_API_KEY is not configured; authenticated API use will fail.')
    if not workspace_exists:
        warnings.append('WORKSPACE path does not exist; server-side storage cannot proceed.')
    elif not workspace_writable:
        warnings.append('WORKSPACE path is not writable; ingest/build-manifest/embed may fail.')
    if workspace_exists:
        if not containers_root_exists:
            warnings.append('containers_root does not exist yet; it will be created on first ingest/build-manifest run.')
        elif not containers_root_writable:
            warnings.append('containers_root is not writable; ingest/build-manifest/embed may fail.')
    if not scripts_dir_exists:
        warnings.append('scripts_dir does not exist; wrapper endpoints cannot run.')
    elif not scripts_dir_writable:
        warnings.append('scripts_dir is not writable; runtime updates to scripts may fail.')
    if not scripts_present['search']:
        warnings.append('task_rag_search.py is missing; search endpoint cannot run.')
    if not scripts_present['embed']:
        warnings.append('task_rag_embed.py is missing; embed endpoint cannot run.')
    if not scripts_present['build_manifest']:
        warnings.append('task_rag_build_manifest.py is missing; build-manifest endpoint cannot run.')
    if not scripts_present['ingest_memory']:
        warnings.append('task_rag_ingest_memory_refs.py is missing; ingest-memory endpoint cannot run.')
    if not embedding_api_key:
        warnings.append('EMBEDDING_API_KEY is not configured; embed/search runtime is not ready.')
    if not faiss_available:
        warnings.append('faiss runtime is unavailable; embed/search runtime is not ready.')

    storage_ready = workspace_exists and workspace_writable and ((not containers_root_exists) or containers_root_writable)

    return {
        'status': 'ok',
        'service': 'transcendence-memory-server',
        'workspace': str(WS),
        'workspace_exists': workspace_exists,
        'workspace_writable': workspace_writable,
        'containers_root': str(containers_root),
        'containers_root_exists': containers_root_exists,
        'containers_root_writable': containers_root_writable,
        'scripts_dir': str(SCRIPTS),
        'scripts_dir_exists': scripts_dir_exists,
        'scripts_dir_writable': scripts_dir_writable,
        'python': sys.executable,
        'auth_configured': bool(RAG_API_KEY),
        'scripts_present': scripts_present,
        'embedding_configured': bool(embedding_api_key),
        'embedding_provider_base_url': embedding_provider_base_url,
        'faiss_available': faiss_available,
        'runtime_ready': {
            'search': scripts_present['search'] and bool(embedding_api_key) and faiss_available,
            'embed': scripts_present['embed'] and bool(embedding_api_key) and faiss_available,
            'build_manifest': scripts_present['build_manifest'] and workspace_exists and workspace_writable,
            'ingest_memory': scripts_present['ingest_memory'] and workspace_exists and workspace_writable,
            'ingest_objects': storage_ready,
        },
        'storage_ready': storage_ready,
        'warnings': warnings,
        'default_container': default_container,
        'available_containers': available_containers,
        'endpoint_defaults': {
            'search.container': default_container,
            'embed.container': default_container,
            'build_manifest.container': default_container,
            'ingest_memory.container': default_container,
            'ingest_objects.container': default_container,
        },
        'uptime_seconds': max(0, int(time.time() - SERVER_STARTED_AT)),
        'server_time_epoch_s': int(time.time()),
        'endpoint_contracts': {
            'search': 'wrapper-command-result',
            'embed': 'wrapper-command-result',
            'build_manifest': 'wrapper-command-result',
            'ingest_memory': 'wrapper-command-result',
            'ingest_objects': 'typed-json-response',
            'health': 'typed-json-response',
            'ingest_contract': 'typed-json-response',
        },
        'server_architecture': {
            'retrieval_mode': 'server-side',
            'ingest_mode': 'client-provided-content-into-server-containers',
            'container_storage_root': str(containers_root),
            'client_side_retrieval_supported': False,
        },
    }


@app.post('/search', response_model=SearchResponse)
def search(req: SearchReq) -> SearchResponse:
    result = run(
        [str(SCRIPTS/'task_rag_search.py'), '--query', req.query, '--topk', str(req.topk), '--container', req.container],
        timeout_s=req.timeout_s,
    )
    parsed_results: list[SearchHit] = []
    if result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    metadata = item.get('metadata')
                    parsed_results.append(SearchHit(
                        path=item.get('path') if isinstance(item.get('path'), str) else None,
                        score=float(item['score']) if isinstance(item.get('score'), (int, float)) else None,
                        text=item.get('text') if isinstance(item.get('text'), str) else None,
                        taskId=item.get('taskId') if isinstance(item.get('taskId'), str) else None,
                        chunkId=item.get('chunkId') if isinstance(item.get('chunkId'), str) else None,
                        docType=item.get('docType') if isinstance(item.get('docType'), str) else None,
                        sourcePath=item.get('sourcePath') if isinstance(item.get('sourcePath'), str) else None,
                        section=item.get('section') if isinstance(item.get('section'), str) else None,
                        metadata=metadata if isinstance(metadata, dict) else {},
                    ))
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed_results = []
    return SearchResponse(
        status='ok' if result.code == 0 else 'error',
        command=result.command,
        code=result.code,
        query=req.query,
        topk=req.topk,
        container=req.container,
        results=parsed_results,
        stdout=result.stdout,
        stderr=result.stderr,
    )


@app.post('/embed', response_model=EmbedResponse)
def embed(req: EmbedRequest) -> EmbedResponse:
    result = run([str(SCRIPTS/'task_rag_embed.py'), '--container', req.container], timeout_s=req.timeout_s)
    return EmbedResponse(**result.model_dump())


@app.post('/build-manifest', response_model=BuildManifestResponse)
def build_manifest(req: BuildManifestRequest) -> BuildManifestResponse:
    result = run([str(SCRIPTS/'task_rag_build_manifest.py'), '--container', req.container], timeout_s=req.timeout_s)
    return BuildManifestResponse(**result.model_dump())


@app.post('/ingest-memory', response_model=IngestMemoryResponse)
def ingest(req: IngestMemoryRequest) -> IngestMemoryResponse:
    cmd = [str(SCRIPTS/'task_rag_ingest_memory_refs.py'), '--container', req.container]
    if req.memory_dir:
        cmd += ['--memory-dir', req.memory_dir]
    if req.archive_dir:
        cmd += ['--archive-dir', req.archive_dir]
    result = run(cmd, timeout_s=req.timeout_s)
    return IngestMemoryResponse(**result.model_dump())


@app.get('/ingest-memory/contract', response_model=IngestContractResponse)
def ingest_contract() -> IngestContractResponse:
    return {
        'mode': 'server-side-container-ingest',
        'content_source': 'client-provided',
        'storage_location': 'Client-provided content is stored in server-side container data under WORKSPACE/tasks/rag/containers/<container>/...',
        'retrieval_scope': 'Retrieval runs server-side against indexed container content; clients submit content but do not perform retrieval locally.',
        'notes': [
            'Use /ingest-memory to ingest client-provided memory references into a server-side container.',
            'Use /ingest-memory/objects for first-class client-provided memory objects stored in server-side containers.',
            'This service keeps retrieval server-side even when content originates from clients.',
        ],
    }


@app.post('/ingest-memory/objects', response_model=ClientIngestResponse)
def ingest_objects(req: ClientIngestReq) -> ClientIngestResponse:
    container_root = WS / 'tasks' / 'rag' / 'containers' / req.container
    container_root.mkdir(parents=True, exist_ok=True)

    stored_paths: list[str] = []
    accepted = 0
    timestamp_ns = time.time_ns()

    for index, obj in enumerate(req.objects, start=1):
        safe_object_id = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in obj.id).strip('-_') or f'object-{index}'
        object_path = container_root / f'client-ingest-{timestamp_ns}-{index:04d}-{safe_object_id}.jsonl'
        record = obj.model_dump(mode='json')
        object_path.write_text(json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8')
        stored_paths.append(str(object_path))
        accepted += 1

    return {
        'container': req.container,
        'accepted': accepted,
        'stored_path': stored_paths[0],
        'stored_paths': stored_paths,
        'index_hint': 'Run /build-manifest and /embed for this container to make newly stored objects searchable.',
    }
