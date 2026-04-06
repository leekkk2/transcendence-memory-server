# Development Bootstrap

## Goal

Get the repository into a locally runnable, understandable, and evolvable `LanceDB-only` server development state with minimal effort.

## Prerequisites

- Python 3.11+ (test files use `dict[str, str] | None` and other 3.10+ syntax; 3.11 is recommended)
- `pytest`
- `httpx`
- `fastapi`
- `uvicorn`
- `requests`
- `numpy`
- `lancedb`
- `pyarrow`

It is recommended to run the in-repo bootstrap first:

```bash
./scripts/bootstrap_dev.sh
```

If your default `python3` is still 3.9, specify the binary explicitly:

```bash
PYTHON_BIN=python3.11 ./scripts/bootstrap_dev.sh
```

The script will:

- Create or reuse `.venv-task-rag-server`
- Default to `python3.11` (overridable via `PYTHON_BIN`)
- Install `pytest` and the minimal dev dependencies
- Share the same virtual environment between `scripts/run_task_rag_server.sh` and `python -m pytest ...`

To install manually instead, the equivalent command is:

```bash
python3 -m pip install pytest httpx fastapi uvicorn requests numpy lancedb pyarrow
```

## Required Environment Variables

This document assumes you are running commands from the `transcendence-memory-server/` repository root:

```bash
export WORKSPACE="$PWD"
export RAG_API_KEY="replace-me"
export EMBEDDING_API_KEY="replace-me"
export EMBEDDING_MODEL="gemini-embedding-001"
export EMBEDDING_BASE_URL="https://api.openai.com/v1"        # read first by runtime
export EMBEDDINGS_BASE_URL="https://api.openai.com/v1"       # canonical name; set both to keep them consistent
export GOOGLE_EMBEDDING_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models"
```

> Note: The actual resolution order at runtime is `EMBEDDING_BASE_URL` -> `EMBEDDINGS_BASE_URL` -> default value.
> If your shell already has an older value for one of these, setting only `EMBEDDINGS_BASE_URL` may not override it. The safest approach during local debugging is to explicitly set both variables to the same endpoint.

## Runtime Directories

The current scripts still use the legacy `tasks/...` directory convention rather than the `tasks_rag/` name found in the repo.
Prepare the minimal local directory structure as follows:

```bash
mkdir -p \
  tasks/active \
  tasks/archived \
  tasks/rag/containers/imac \
  memory \
  memory_archive
```

## Starting the Service

```bash
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## Minimal Smoke Test

```bash
curl -sS http://127.0.0.1:8711/health
```

## Typed Object Proof Smoke

To verify that typed client objects can be stably written to canonical storage, run:

```bash
python3 scripts/smoke_test_client_ingest_search.py
```

The script will:

- Create a temporary `WORKSPACE`
- Write typed objects via `POST /ingest-memory/objects`
- Directly inspect `memory_objects.jsonl`

## Pytest Verification

To verify the full `LanceDB-only` retrieval pipeline, run:

```bash
python3 -m pytest tests/test_task_rag_server_memory_objects.py -q
```

The test will:

- Create a temporary `WORKSPACE`
- Start a local fake embedding HTTP service
- Write typed objects via `POST /ingest-memory/objects`
- Call `POST /embed` to rebuild LanceDB
- Call `POST /search` to verify the objects are retrievable

## Known Constraints

- `embed` and `search` depend on `lancedb` and `pyarrow`
- The server still retains script-wrapper observability fields, but the main pipeline architecture has been unified to `LanceDB-only`
- `POST /build-manifest` has been removed from the main pipeline and is retained only as a deprecated no-op
- Directory and script names still carry the legacy `task_rag_*` semantics; renaming is out of scope for this cycle
