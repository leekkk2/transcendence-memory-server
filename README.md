# Transcendence Memory Server

> Private server-side implementation layer for the transcendence-memory system.

## Positioning

This repository is the **private server implementation** behind the broader transcendence-memory platform.

It should be understood as:
- the hosted memory service runtime
- the private indexing / retrieval / ingestion backend
- the place for server-side deployment-facing implementation

It should **not** be treated as:
- the entire product abstraction
- the client enhancer skill
- the long-running workspace control plane

## Relationship to the wider system

- **Workspace control plane**: `transcendence-memory-workspace`
- **Private server implementation**: this repo (`transcendence-memory-server`)
- **Private skill artifact repo**: `skills-hub`
- **Future public abstraction layer**: `transcendence-memory`

## Current scope

The current server provides a lightweight task/memory retrieval service centered on:
- search
- embed
- manifest building
- memory ingestion

Current implementation artifacts include:
- `scripts/task_rag_server.py`
- `scripts/task_rag_search.py`
- `scripts/task_rag_embed.py`
- `scripts/task_rag_build_manifest.py`
- `scripts/task_rag_ingest_memory_refs.py`
- `tasks_rag/`

## Current runtime model

Primary deployment target today:
- hosted on Eva
- exposed behind `https://rag.zweiteng.tk`

## Documentation entry points

- `docs/server-boundary.md`
- `docs/api-contract.md`
- `docs/development-bootstrap.md`
- `docs/nginx-rag.zweiteng.tk.conf`

## Notes on naming

The local folder name may still be `rag-everything` in some workspaces for continuity, but the remote/server role is now aligned to **transcendence-memory-server**.

## Security

Do not commit tokens, secrets, or private keys into this repository.
