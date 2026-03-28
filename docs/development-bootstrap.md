# Development Bootstrap

## Goal
Help future AI sessions or developers resume work on the private server repo quickly.

## Start here
1. Read `README.md`
2. Read `docs/server-boundary.md`
3. Read `docs/api-contract.md`
4. Inspect `scripts/task_rag_server.py`
5. Inspect current helper scripts under `scripts/`

## Current implementation baseline
- FastAPI wrapper in `scripts/task_rag_server.py`
- CLI-oriented helper scripts for search/embed/build-manifest/ingest
- `tasks_rag/` as current storage-oriented area

## Immediate development priorities
1. make naming and docs reflect the server role clearly
2. preserve current working behavior
3. gradually replace script-envelope responses with cleaner API contracts
4. prepare the codebase for broader memory object support

## What not to do first
- do not start with a big rewrite
- do not rename every local directory immediately
- do not move client/workspace concerns into this repo

## Suggested next implementation step
A strong next step is to add explicit response models and a health endpoint while preserving current operational behavior.
