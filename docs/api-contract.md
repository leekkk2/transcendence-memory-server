# API Contract Draft

## Status

Draft — consolidated mainline contract after branch unification.

## Goal

Provide one canonical private server contract for Transcendence Memory server-side retrieval.

## Authentication

Requests should provide either:

- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

## Runtime assumptions

Current documented runtime assumes:

- `WORKSPACE` points at the active runtime root that contains `tasks/`, `memory/`, and `memory_archive/`
- `RAG_API_KEY` is set for protected endpoints
- `EMBEDDING_API_KEY` is set for embedding-backed endpoints
- embedding provider base URL currently resolves with `EMBEDDING_BASE_URL` first, then `EMBEDDINGS_BASE_URL`, then the built-in default
- to avoid shell-state ambiguity during local debugging, prefer setting both `EMBEDDING_BASE_URL` and `EMBEDDINGS_BASE_URL` to the same value

Minimal documented local startup path:

```bash
./scripts/bootstrap_dev.sh
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## Canonical architecture

- Retrieval is server-side only
- Backend indexing is `LanceDB-only`
- Canonical source types are:
  - task cards under `tasks/active` and `tasks/archived`
  - markdown memory under configured memory directories
  - typed client objects persisted in `memory_objects.jsonl`
  - structured JSON-like payloads ingested through `POST /ingest-structured`

## Current endpoints

### `GET /health`

Anonymous health probe for runtime verification.

Current response highlights:

- `architecture: "lancedb-only"`
- `auth_configured`
- `embedding_configured`
- `lancedb_available`
- `scripts_present`
- `runtime_ready`
- `available_containers`
- `warnings`

### `POST /search`

Search indexed memory for a container.

Request:

```json
{
  "query": "string",
  "topk": 5,
  "container": "imac",
  "timeout_s": 120
}
```

Response shape:

```json
{
  "status": "ok",
  "command": ["python3", "/path/to/task_rag_search.py", "..."],
  "code": 0,
  "query": "string",
  "topk": 5,
  "container": "imac",
  "initialized": true,
  "message": null,
  "results": [
    {
      "score": 0.12,
      "taskId": "TASK-20260329-004",
      "docType": "client_ingest",
      "sourcePath": "tasks/rag/containers/imac/memory_objects.jsonl",
      "text": "..."
    }
  ],
  "stdout": "{...raw search payload...}",
  "stderr": ""
}
```

### `POST /embed`

Rebuild canonical task-card, markdown-memory, and typed-object rows for a container into LanceDB.

Request:

```json
{
  "container": "imac",
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

Notes:

- This is the canonical rebuild entrypoint
- Existing structured-ingest rows are retained during rebuild

### `POST /ingest-memory`

Run canonical LanceDB rebuild with optional explicit memory/archive source directories.

Request:

```json
{
  "container": "imac",
  "memory_dir": null,
  "archive_dir": null,
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

### `GET /ingest-memory/contract`

Expose the current ingest semantic boundary explicitly.

Current response shape:

```json
{
  "mode": "lancedb-only",
  "content_source": "server-side-canonical-sources",
  "storage_location": "Canonical LanceDB rows live under WORKSPACE/tasks/rag/containers/<container>/lancedb.",
  "retrieval_scope": "Retrieval runs server-side against LanceDB only.",
  "notes": [
    "Use /ingest-memory/objects to persist typed objects into canonical server-side storage.",
    "Use /embed to rebuild task-card, markdown-memory, and typed-object rows into LanceDB.",
    "Use /ingest-structured for direct structured JSON-like ingest into LanceDB."
  ]
}
```

### `POST /ingest-memory/objects`

Persist typed client objects into canonical server-side storage for a container.

Request:

```json
{
  "container": "imac",
  "objects": [
    {
      "id": "memory-001",
      "text": "Client-provided retrievable text.",
      "title": "Optional title",
      "source": "telegram",
      "tags": ["project_fact"],
      "metadata": {
        "project": "transcendence-memory"
      }
    }
  ]
}
```

Response shape:

```json
{
  "container": "imac",
  "accepted": 1,
  "stored_path": "/workspace/tasks/rag/containers/imac/memory_objects.jsonl",
  "stored_paths": ["/workspace/tasks/rag/containers/imac/memory_objects.jsonl"],
  "index_hint": "Run /embed for this container to refresh LanceDB after storing new objects."
}
```

### `POST /ingest-structured`

Parse JSON-like payloads into semantic chunks and upsert them into LanceDB.

Request:

```json
{
  "container": "eva",
  "input_path": "/path/to/bookmarks.json",
  "doc_type": "structured_json",
  "doc_id": "chrome-bookmarks",
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

### `POST /build-manifest`

Deprecated no-op retained only to make the exit from the old manifest phase explicit.

Response shape:

```json
{
  "command": [],
  "code": 0,
  "status": "deprecated",
  "note": "build-manifest was removed in LanceDB-only mode; use /embed."
}
```

## Repository evidence

- `tests/test_task_rag_server_memory_objects.py` covers typed object persistence plus `/embed -> /search` retrieval on the LanceDB-only path
- `scripts/smoke_test_client_ingest_search.py` verifies typed client objects persist to canonical `memory_objects.jsonl`
- `docs/evolution/*` records how the repo moved from early manifest/FAISS stages to the current mainline
