# API Contract Draft

## Status
Draft — initial server-side contract framing.

## Goal
Provide a minimal contract surface for the current private memory server while leaving room for later expansion beyond task-only retrieval.

Requests should provide either:
- `X-API-Key: <RAG_API_KEY>`
- or `Authorization: Bearer <RAG_API_KEY>`

## Current endpoints

Unless otherwise noted, the current FastAPI wrapper endpoints return the same subprocess envelope:

```json
{
  "code": 0,
  "stdout": "...script output...",
  "stderr": ""
}
```

### `POST /search`
Search indexed memory for a container.

Request:
```json
{
  "query": "string",
  "topk": 5,
  "container": "imac"
}
```

### `POST /embed`
Trigger embedding/index refresh for a container.

Request:
```json
{
  "container": "imac"
}
```

### `POST /build-manifest`
Build or rebuild manifest material for a container.

Current implementation writes `tasks/rag/containers/<container>/manifest.jsonl` and merges:
- task-card section chunks
- persisted memory-object records from server-side memory object ingest storage

Request:
```json
{
  "container": "imac"
}
```

### `POST /ingest-memory`
Ingest memory references for a container.

Legacy status:
- This endpoint is still a legacy filesystem ingest entry.
- Current repository memory-object proofs no longer use it as the primary path.

Request:
```json
{
  "container": "imac",
  "memory_dir": null,
  "archive_dir": null
}
```

### `POST /ingest-memory/objects`
Persist memory objects for a container.

Current implementation is **typed-first** for client-provided objects. Filesystem-based ingest for markdown memory refs stays on the separate legacy endpoint `POST /ingest-memory`; `POST /ingest-memory/objects` no longer accepts `memory_dir/archive_dir` fallback input.

Typed-first request:
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
  ],
  "timeout_s": 120
}
```

Typed response when `objects` is provided:
```json
{
  "container": "imac",
  "accepted": 1,
  "stored_path": ".../client-ingest-...jsonl",
  "stored_paths": [".../client-ingest-...jsonl"],
  "index_hint": "Run /build-manifest and /embed for this container to make newly stored objects searchable."
}
```

Compatibility note:
- Filesystem ingest remains available only through the separate legacy endpoint `POST /ingest-memory`.
- `POST /ingest-memory/objects` now requires typed `objects` input and should be treated as the target client-ingest contract.

Repository evidence from the current tree:
- `tests/test_task_rag_server_memory_objects.py` covers typed `objects: [...]` ingest, manifest merge, and `/embed -> /search` retrieval.
- `scripts/smoke_test_client_ingest_search.py` posts typed `objects` to `/ingest-memory/objects` and checks the full ingest/build-manifest/embed/search loop.
- `scripts/smoke_test_client_ingest_wrapper_flow.py` calls `ingest_objects(ClientIngestReq(objects=[...]))` and checks the wrapper path on top of typed ingest.
- `docs/development-bootstrap.md` documents the typed-object smoke flow and does not document a `memory_dir/archive_dir` call for `/ingest-memory/objects`.
- A repository-wide search currently finds no test, smoke script, or doc example that sends `memory_dir` or `archive_dir` to `/ingest-memory/objects`.

Current legacy entry that still remains:
- `POST /ingest-memory`

## Memory object status

Current code already supports a lightweight memory-object flow:
- `POST /ingest-memory/objects` persists client-provided typed objects into server-side container storage
- `POST /build-manifest` merges those records into container `manifest.jsonl` as `client_ingest` entries
- `POST /embed` followed by `POST /search` can retrieve those ingested objects

Current repository-level test coverage proves:
- typed `objects: [...]` ingest + manifest merge for client-ingested records
- a minimal `/embed -> /search` retrieval proof for typed client-ingested objects

Both checks currently live in `tests/test_task_rag_server_memory_objects.py`.

What is still missing is a fully typed service surface across the whole server. Some endpoints still expose subprocess-wrapper responses rather than explicit JSON domain models.

## Longer-term direction

Longer-term, the server should evolve beyond task-only chunks toward first-class memory objects such as:
- task_state
- decision
- conversation_summary
- project_fact
- user_preference
- design_note
- deployment_note

A future contract should expose typed objects and retrieval results rather than only script-wrapped command output.

## Near-term improvement target

The next contract cleanup should move from raw script subprocess envelopes toward a typed service response with:
- `results`
- `errors`
- `meta`

Repository-level tests now include a minimal end-to-end proof that ingested memory objects become searchable through `/search` after manifest rebuild and embedding refresh.
