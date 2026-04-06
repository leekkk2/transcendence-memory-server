# Deployment Quickstart

## Identity First

This page is intended for **backend identity** or **the backend phase of a both identity**.

- If the current machine is `frontend`, refer to the client documentation in the transcendence-memory skill
- Check the local `operator-identity.md` first
- If the identity document is missing, complete identity registration before proceeding with deployment

## Prerequisites

```bash
python3 --version    # >= 3.11
docker --version     # Optional, required for Docker deployment
docker compose version
```

Verify:
- Python meets the repository requirement (currently `>=3.11`)
- If using Docker deployment, Docker / Docker Compose are available
- Network/proxy paths are available (needed when pulling images or external dependencies)
- Whether the current session can directly access the host Docker daemon

## Shortest Startup Path (Bare Metal)

```bash
cd transcendence-memory-server

# 1. Bootstrap development environment
./scripts/bootstrap_dev.sh

# 2. Set environment variables
export WORKSPACE="$PWD"
export RAG_API_KEY="replace-me"
export EMBEDDING_API_KEY="replace-me"
export EMBEDDING_BASE_URL="https://your-embedding-endpoint/v1"
export EMBEDDINGS_BASE_URL="https://your-embedding-endpoint/v1"

# 3. Prepare runtime directories
mkdir -p tasks/active tasks/archived tasks/rag/containers/imac memory memory_archive

# 4. Start the service
./scripts/run_task_rag_server.sh
# Or manually:
# uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711

# 5. Health check
curl -sS http://127.0.0.1:8711/health
```

## Docker Deployment

See [docker-deployment.md](docker-deployment.md) for details.

The default build target is `lite`. If the next step involves multimodal parsing or the `rag-everything` pipeline, explicitly set the build target before starting:

```bash
BUILD_TARGET=full docker compose up -d --build
```

## Reverse Proxy

See [reverse-proxy.md](reverse-proxy.md) for details.

## Environment Variables

See [environment-reference.md](environment-reference.md) for the full reference.

## Current Runtime Specifications

- Default port: `8711`
- Default build target: **lite**
- Runtime architecture: dynamic detection based on key + package availability
- Authentication: `X-API-KEY` header or `Authorization: Bearer`

## Information to Hand Off to Frontend After Deployment

After backend deployment is complete, do not just give the frontend a URL. At minimum, provide the following:

1. Connection materials (prefer using the server's native `/export-connection-token` response; if a standalone backend CLI is still available, additionally export the bundle)
2. The authentication mode the frontend should use
3. Authentication materials the frontend still needs to prepare locally
4. The command sequence the frontend should execute next

If using the server's native `/export-connection-token` flow, pass the `pairing_auth` and `agent_onboarding` from the response to the integrating AI, rather than just forwarding a token.

If the project still has a standalone `backend export-connection` CLI, treat it as a compatibility fallback rather than the primary entry point.

## Backend Acceptance

Verify at minimum:
- `GET /health` → 200
- `POST /search` → 200 + real results
- `POST /embed` → 200 + success
- If the target pipeline depends on typed ingest, also verify `/ingest-memory/objects`

## Troubleshooting

Check the following first:
1. Whether the current documentation still matches the canonical backend runtime
2. Whether the environment meets Python / Docker / network prerequisites
3. Whether the current session can access the Docker daemon
4. Whether the advertised endpoint is correct
5. Whether the handoff / auth / smoke paths are complete

See [troubleshooting.md](../operations/troubleshooting.md) for details.
