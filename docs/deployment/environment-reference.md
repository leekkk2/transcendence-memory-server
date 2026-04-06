# Environment Variable Reference

## Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `WORKSPACE` | Runtime root directory containing `tasks/`, `memory/`, etc. | `$PWD` (server repository root) |
| `RAG_API_KEY` | API authentication key | `sk-xxx` |
| `EMBEDDING_API_KEY` | Embedding provider key | `sk-xxx` |

## Embedding Variables

| Variable | Description | Priority |
|----------|-------------|----------|
| `EMBEDDING_BASE_URL` | Embedding provider endpoint | Highest (read first by runtime) |
| `EMBEDDINGS_BASE_URL` | Embedding provider endpoint (canonical name) | Second highest |
| `EMBEDDING_MODEL` | Embedding model name | `gemini-embedding-001` |
| `GOOGLE_EMBEDDING_BASE_URL` | Google embedding endpoint (fallback) | — |

> **Note**: The current runtime resolution order is `EMBEDDING_BASE_URL` → `EMBEDDINGS_BASE_URL` → default value.
> During local debugging, it is recommended to set both variables to the same value to avoid ambiguity caused by stale values in the shell environment.

## RAG Configuration Loading

If using `load_rag_config.sh` to load configuration:

```bash
source ./scripts/load_rag_config.sh
```

This script exports the following from `~/.config/transcendence-memory/rag-config.json` (or the path overridden by `RAG_CONFIG_FILE`):
- `RAG_ENDPOINT`
- `RAG_AUTH_HEADER`
- `RAG_API_KEY`
- `RAG_DEFAULT_CONTAINER`

## Runtime Directory Structure

```
$WORKSPACE/
├── tasks/
│   ├── active/          # Active task cards
│   ├── archived/        # Archived task cards
│   └── rag/
│       └── containers/
│           └── <name>/  # LanceDB data for each container
├── memory/              # Markdown memory files
└── memory_archive/      # Archived memory
```

## Service Port

| Setting | Default |
|---------|---------|
| Listen port | `8711` |
| Listen address | `0.0.0.0` |

## Authentication

Two header formats are supported:
- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

The `/health` endpoint allows anonymous access; business endpoints require authentication.
