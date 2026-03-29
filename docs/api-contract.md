# API Contract Draft

## Status
Draft — current private server contract framing, aligned to the existing script implementation.

## Goal
Provide a minimal contract surface for the current private memory server while leaving room for later expansion beyond task-only retrieval.

## Authentication
Requests should provide either:
- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

## Current endpoints

### `GET /health`
Basic authenticated health probe for runtime verification.

Current response shape:
```json
{
  "status": "ok",
  "service": "transcendence-memory-server",
  "workspace": "/path/to/workspace",
  "workspace_exists": true,
  "workspace_writable": true,
  "containers_root": "/path/to/workspace/tasks/rag/containers",
  "scripts_dir": "/path/to/workspace/scripts",
  "scripts_dir_exists": true,
  "scripts_dir_writable": true,
  "python": "/usr/bin/python3",
  "auth_configured": true,
  "scripts_present": {
    "search": true,
    "embed": true,
    "build_manifest": true,
    "ingest_memory": true
  },
  "embedding_configured": true,
  "embedding_provider_base_url": "https://newapi.zweiteng.tk/v1",
  "faiss_available": true,
  "runtime_ready": {
    "search": true,
    "embed": true,
    "build_manifest": true,
    "ingest_memory": true,
    "ingest_objects": true
  },
  "storage_ready": true,
  "warnings": [],
  "default_container": "imac",
  "available_containers": ["imac", "eva"],
  "endpoint_defaults": {
    "search.container": "imac",
    "embed.container": "imac",
    "build_manifest.container": "imac",
    "ingest_memory.container": "imac",
    "ingest_objects.container": "imac"
  },
  "uptime_seconds": 42,
  "server_time_epoch_s": 1774743300,
  "endpoint_contracts": {
    "search": "wrapper-command-result",
    "embed": "wrapper-command-result",
    "build_manifest": "wrapper-command-result",
    "ingest_memory": "wrapper-command-result",
    "ingest_objects": "typed-json-response",
    "health": "typed-json-response",
    "ingest_contract": "typed-json-response"
  },
  "server_architecture": {
    "retrieval_mode": "server-side",
    "ingest_mode": "client-provided-content-into-server-containers",
    "container_storage_root": "/path/to/workspace/tasks/rag/containers",
    "client_side_retrieval_supported": false
  }
}
```

Health semantics:
- `auth_configured`: whether `RAG_API_KEY` is configured in the server process
- `workspace_exists`: confirms the configured `WORKSPACE` path exists
- `workspace_writable`: confirms the process can write to `WORKSPACE`
- `containers_root`: resolved path to `WORKSPACE/tasks/rag/containers`
- `scripts_dir`: resolved path to the current runtime scripts directory
- `scripts_dir_exists`: whether the runtime scripts directory exists
- `scripts_dir_writable`: whether the runtime scripts directory is writable
- `scripts_present`: verifies the expected script entrypoints still exist for the current wrapper-based runtime
- `python`: exposes the current interpreter path for deployment/runtime debugging
- `embedding_configured`: whether `EMBEDDING_API_KEY` is present for embed/search operations
- `embedding_provider_base_url`: shows which embedding provider base URL is currently configured
- `faiss_available`: reports whether the FAISS runtime dependency can be imported
- `runtime_ready`: endpoint-level readiness summary for the current runtime; `search` and `embed` require scripts + embedding config + FAISS, while `ingest_objects` requires writable server-side storage
- `storage_ready`: whether the server-side WORKSPACE/container storage path is writable enough for first-class client object ingest
- `warnings`: human-readable readiness problems for operators (missing auth, missing scripts, unwritable workspace, missing embedding runtime, etc.)
- `default_container`: current server default container used when callers omit an explicit container
- `available_containers`: currently discovered server-side container directories under `WORKSPACE/tasks/rag/containers`
- `endpoint_defaults`: machine-readable default container mapping for each container-scoped endpoint
- `uptime_seconds`: process-local uptime for the current API server instance
- `server_time_epoch_s`: current server wall-clock time for operator/debug correlation
- `endpoint_contracts`: explicit per-endpoint contract classification so operators can see which routes are still wrapper-command envelopes vs typed JSON service responses (`search` is now a typed JSON response that still preserves wrapper observability fields)
- `server_architecture`: explicit runtime statement that retrieval stays server-side while clients provide content into server-side containers

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

Current response shape:
```json
{
  "status": "ok",
  "command": ["/path/to/script.py", "--query", "server-side retrieval", "--topk", "5", "--container", "imac"],
  "code": 0,
  "query": "server-side retrieval",
  "topk": 5,
  "container": "imac",
  "results": [
    {
      "path": "tasks/rag/containers/imac/manifest.jsonl",
      "score": 0.99,
      "text": "..."
    }
  ],
  "stdout": "...json string...",
  "stderr": ""
}
```

Search response semantics:
- `status`: `ok` when the underlying command exits with `code == 0`, otherwise `error`
- `command`: executed command argv list for observability/compatibility
- `code`: subprocess exit code (`124` for timeout, `127` if the script entrypoint is missing)
- `query`, `topk`, `container`: explicit typed echo of the request contract
- `results`: typed search hits parsed from the underlying JSON list; each hit can expose `path`, `score`, `text`, `taskId`, `chunkId`, `docType`, `sourcePath`, `section`, and a fallback `metadata` object
- `stdout`: raw stdout from the underlying script (preserved for compatibility)
- `stderr`: raw stderr from the underlying script

### `POST /embed`
Trigger embedding/index refresh for a container.

Request:
```json
{
  "container": "imac",
  "timeout_s": 120
}
```

### `POST /build-manifest`
Build or rebuild manifest material for a container.

Request:
```json
{
  "container": "imac",
  "timeout_s": 120
}
```

### `POST /ingest-memory`
Ingest memory references for a container.

Request:
```json
{
  "container": "imac",
  "timeout_s": 120,
  "memory_dir": null,
  "archive_dir": null
}
```

### `GET /ingest-memory/contract`
Expose the current ingest semantic boundary explicitly.

Current response shape:
```json
{
  "mode": "server-side-container-ingest",
  "content_source": "client-provided",
  "storage_location": "Client-provided content is stored in server-side container data under WORKSPACE/tasks/rag/containers/<container>/...",
  "retrieval_scope": "Retrieval runs server-side against indexed container content; clients submit content but do not perform retrieval locally.",
  "notes": [
    "Use /ingest-memory to ingest client-provided memory references into a server-side container.",
    "Use /ingest-memory/objects for first-class client-provided memory objects stored in server-side containers.",
    "This service keeps retrieval server-side even when content originates from clients."
  ]
}
```

### `POST /ingest-memory/objects`
Persist client-provided memory objects directly into a server-side container in JSONL form.

Request:
```json
{
  "container": "imac",
  "objects": [
    {
      "id": "note-001",
      "text": "User prefers server-side retrieval.",
      "title": "retrieval preference",
      "source": "client-app",
      "tags": ["preference", "retrieval"],
      "metadata": {
        "project": "transcendence-memory",
        "kind": "decision"
      }
    }
  ]
}
```

Current response shape:
```json
{
  "container": "imac",
  "accepted": 2,
  "stored_path": "/workspace/tasks/rag/containers/imac/client-ingest-1711765800000000000-0001-note-001.jsonl",
  "stored_paths": [
    "/workspace/tasks/rag/containers/imac/client-ingest-1711765800000000000-0001-note-001.jsonl",
    "/workspace/tasks/rag/containers/imac/client-ingest-1711765800000000000-0002-note-002.jsonl"
  ],
  "index_hint": "Run /build-manifest and /embed for this container to make newly stored objects searchable."
}
```

Current semantics:
- writes each submitted object into its own server-side JSONL file immediately for traceable storage
- `stored_path` remains as the first stored file for compatibility, while `stored_paths` exposes the full per-object storage set
- preserves server-side retrieval architecture
- provides a first-class client ingest path without breaking legacy `/ingest-memory`
- `/build-manifest` now folds persisted `client-ingest-*.jsonl` objects into `manifest.jsonl`
- generated `client_ingest` manifest chunks include title/body plus lightweight textualized tags/metadata (`project`, `kind`, `status`, `source`) so retrieval can match on contract metadata, not only the raw body text
- `/embed` remains the explicit follow-up step that makes those manifest entries searchable
- end-to-end searchability still depends on embedding runtime configuration (for example `EMBEDDING_API_KEY`); without that, `/search` may fail even when ingest + manifest generation succeeded

## Memory object direction

The current manifest/chunk shape can already be treated as a draft server-side memory object:

```json
{
  "chunkId": "TASK-20260328-001#Decision",
  "taskId": "TASK-20260328-001",
  "docType": "task_card",
  "sourcePath": "tasks/active/feature/TASK-20260328-001.md",
  "section": "Decision",
  "text": "决定继续沿用私有 server 作为主入口。",
  "tags": ["memory", "server"],
  "project": "transcendence-memory",
  "status": "active",
  "createdAt": "2026-03-28",
  "updatedAt": "2026-03-28"
}
```

Current field notes:
- `chunkId`: unique chunk identifier
- `taskId`: task identifier; `memory_ref` records infer this from text matches
- `docType`: currently `task_card` or `memory_ref`
- `sourcePath`: source file path; `memory_ref` may currently emit absolute paths
- `section`: source section name or `memory_ref`
- `text`: indexed content payload
- `tags`, `project`, `status`, `createdAt`, `updatedAt`: optional metadata copied from task cards when available

Longer-term, the server should evolve beyond task-only chunks toward first-class memory objects such as:
- task_state
- decision
- handoff
- troubleshooting_note
- design_note
- deployment_note

A future contract should expose typed objects rather than only script-wrapped command output.

## Near-term improvement target

The next contract cleanup should move from raw script subprocess envelopes toward explicit JSON response models such as:
- `status`
- `results`
- `errors`
- `meta`
