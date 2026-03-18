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
from pydantic import BaseModel
import subprocess
from pathlib import Path

import os

WS = Path(os.environ.get('WORKSPACE', '/home/ubuntu/.openclaw/workspace'))
SCRIPTS = WS / 'scripts'

RAG_API_KEY = os.environ.get('RAG_API_KEY', '')


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
    query: str
    topk: int = 5
    container: str = 'imac'

class ContainerReq(BaseModel):
    container: str = 'imac'

class IngestReq(BaseModel):
    container: str = 'imac'
    memory_dir: str | None = None
    archive_dir: str | None = None


def run(cmd: list[str]):
    p = subprocess.run(cmd, capture_output=True, text=True)
    return {"code": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

@app.post('/search')
def search(req: SearchReq):
    return run([str(SCRIPTS/'task_rag_search.py'), '--query', req.query, '--topk', str(req.topk), '--container', req.container])

@app.post('/embed')
def embed(req: ContainerReq):
    return run([str(SCRIPTS/'task_rag_embed.py'), '--container', req.container])

@app.post('/build-manifest')
def build_manifest(req: ContainerReq):
    return run([str(SCRIPTS/'task_rag_build_manifest.py'), '--container', req.container])

@app.post('/ingest-memory')
def ingest(req: IngestReq):
    cmd = [str(SCRIPTS/'task_rag_ingest_memory_refs.py'), '--container', req.container]
    if req.memory_dir:
        cmd += ['--memory-dir', req.memory_dir]
    if req.archive_dir:
        cmd += ['--archive-dir', req.archive_dir]
    return run(cmd)
