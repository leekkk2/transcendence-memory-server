# API Contract Draft

## Status
Draft — initial server-side contract framing

## Goal
Provide a minimal contract surface for the current private memory server while leaving room for later expansion beyond task-only retrieval.

## Authentication
Requests should provide either:
- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

## Current endpoints

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

Current response shape:
```json
{
  "code": 0,
  "stdout": "...json string...",
  "stderr": ""
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

Request:
```json
{
  "container": "imac"
}
```

### `POST /ingest-memory`
Ingest memory references for a container.

Request:
```json
{
  "container": "imac",
  "memory_dir": null,
  "archive_dir": null
}
```

## Memory object direction

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
