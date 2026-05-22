# Transcendence Memory Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)

> **Self-hosted multimodal RAG cloud memory service — a shared brain for your AI agents.**

[中文文档](README.zh-CN.md)

Transcendence Memory Server is a cloud memory backend that multiple AI agents connect to simultaneously. Each agent stores its own memories in isolated containers, while being able to cross-query other agents' knowledge — turning isolated AI sessions into a collaborative, persistent knowledge network.

```
  Agent A (Claude Code)          Agent B (Codex CLI)          Agent C (OpenClaw)
       |                              |                              |
       |  store & search own          |  store & search own          |  store & search own
       |  cross-query B, C            |  cross-query A, C            |  cross-query A, B
       |                              |                              |
       +------------------------------+------------------------------+
                                      |
                         Transcendence Memory Server
                         +-------------------------+
                         |  Container: agent-a      |
                         |  Container: agent-b      |
                         |  Container: agent-c      |
                         |  Container: shared       |
                         +-------------------------+
```

## AI-Assisted Setup (Simple Edit & Go)

Don't want to read the docs? Copy the prompt below, fill in the `<PLACEHOLDERS>` with your own values, and paste it to your AI assistant (Claude Code, Codex CLI, Cursor, etc.) — it will handle the rest.

<details>
<summary><strong>Click to expand the prompt template</strong></summary>

```text
Please install and configure transcendence-memory-server for me:

1. Repository:
   https://github.com/leekkk2/transcendence-memory-server

2. Deployment target:
   • Service domain: <YOUR_DOMAIN>          # e.g. memory.example.com, or "localhost" for local-only
   • Reverse proxy: Nginx                    # remove this line if local-only
   • Backend listen: 127.0.0.1:8711
   • Public URL: https://<YOUR_DOMAIN>       # remove if local-only

3. Build flavor (pick one):
   • lite   — default, text memory + vector search + knowledge graph
   • full   — lite + multimodal (PDF/image/table parsing via RAG-Anything)

4. LLM / Embedding / Vision config:
   • LLM_BASE_URL=<YOUR_LLM_ENDPOINT>       # e.g. https://api.openai.com/v1
   • LLM_API_KEY=<YOUR_LLM_KEY>
   • LLM_MODEL=<YOUR_LLM_MODEL>             # e.g. gpt-4o, claude-sonnet-4-20250514, gemini-2.5-flash
   • EMBEDDING_BASE_URL=<YOUR_EMBED_ENDPOINT>
   • EMBEDDING_API_KEY=<YOUR_EMBED_KEY>
   • EMBEDDING_MODEL=<YOUR_EMBED_MODEL>      # e.g. text-embedding-3-small, gemini-embedding-001
   • VLM_API_KEY=<YOUR_VLM_KEY>              # optional, only needed for "full" build
   • VLM_MODEL=<YOUR_VLM_MODEL>              # e.g. gpt-4o, qwen3-vl-plus

5. Deployment requirements:
   • Build flavor: <lite or full>
   • Write .env correctly
   • Set RAG_ADVERTISED_ENDPOINT=https://<YOUR_DOMAIN>   # remove if local-only
   • Ensure service runs persistently
   • Nginx reverse proxy to 127.0.0.1:8711               # remove if local-only

6. Post-install verification:
   • Local health check:  http://127.0.0.1:8711/health
   • Public health check: https://<YOUR_DOMAIN>/health    # remove if local-only

7. After installation, output:
   • Actual deployment path
   • Actual listen port
   • Health check result
   • Connection string for the client skill
   • Default container name: <YOUR_CONTAINER>  # e.g. eva, my-agent

Execute install, configure, start, verify, and output the final usable result.
Do not omit the connection string.
```

</details>

> **Tip**: Remove lines marked `# remove if local-only` when deploying on localhost without a domain. For the minimal setup (vector search only), you only need `EMBEDDING_*` keys — `LLM_*` and `VLM_*` are optional and unlock higher [architecture tiers](#architecture-tiers).

## Why Cloud Memory?

| Problem | Without | With Transcendence |
|---------|---------|-------------------|
| Session ends | Memory lost | Persisted to cloud, recoverable anytime |
| Switch agents | Start from zero | New agent inherits context via search |
| Cross-project | Knowledge siloed | Agent B queries Agent A's decisions |
| Team of agents | Each works in isolation | Shared container for collective knowledge |
| Onboarding | Re-explain everything | Agent reads past decisions and rationale |

## Features

- **Multi-Agent Cloud Memory** — one server, many agents; each stores its own, each can query others
- **Lite / Full Build Flavors** — default `lite` image, optional `full` image for multimodal dependencies
- **Container Isolation** — per-agent or per-project namespaces with full CRUD; shared containers for team knowledge
- **LanceDB Vector Search** — sub-second semantic retrieval over task cards, memory objects, and structured data
- **LightRAG Knowledge Graph** — entity/relation extraction with hybrid retrieval (local + global + keyword)
- **RAG-Anything Multimodal** — PDF, image, and table parsing with vision model support
- **Auto-Detect Architecture** — automatically enables capabilities based on configured API keys
- **Connection Token** — one-step client setup; give each agent a token and it's connected
- **Zero Permission Issues** — Docker named volumes, no bind mount headaches

## Build Flavors

The server now exposes two build flavors:

| Flavor | Default | Includes |
|--------|---------|----------|
| `lite` | Yes | FastAPI, LanceDB, LightRAG, typed ingest, connection token export |
| `full` | No | `lite` + `raganything` multimodal dependencies |

Switch flavors at build time:

```bash
# default
docker compose up -d --build

# full multimodal build
BUILD_TARGET=full docker compose up -d --build
```

`/health` reports the active `build_flavor`, whether the runtime is `multimodal_capable`, and any `degraded_reasons`.

## Platform Support

 - **Python package** — CI currently validates `Linux` and `Windows` on Python `3.11`, `3.12`, `3.13`
- **Docker images** — published for `linux/amd64` and `linux/arm64`
- **macOS / Windows hosts** — supported through Docker Desktop running Linux containers
- **Native non-Linux containers** — no native macOS container image exists, and no native Windows container image is published for this project

## Architecture Tiers

The server auto-detects its capability tier based on your `.env` configuration:

| Tier | Required Keys | Capabilities |
|------|--------------|-------------|
| `lancedb-only` | `EMBEDDING_API_KEY` | Vector search, typed objects, structured ingest |
| `lancedb+lightrag` | + `LLM_API_KEY` | + Knowledge graph, entity extraction, hybrid queries |
| `rag-everything` | + `VLM_API_KEY` | + PDF/image/table parsing, vision model queries |

## Quick Start

### Docker (recommended)

Docker Desktop on macOS and Windows is supported as long as it is running Linux containers. Intel hosts will typically pull `linux/amd64`; Apple Silicon and Windows on Arm can pull `linux/arm64`.

```bash
git clone https://github.com/leekkk2/transcendence-memory-server.git
cd transcendence-memory-server
cp .env.example .env    # edit with your API keys
# optional: BUILD_TARGET=full for multimodal package set
docker compose up -d --build
curl http://localhost:8711/health
```

### Production (VPS + Nginx)

```bash
# Preflight check
bash scripts/preflight_check.sh

# optional: BUILD_TARGET=full
# Deploy with localhost-only binding
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### Connect Your Agents

Once the server is running, each agent gets its own connection token:

```bash
# Export a token for Agent A
curl -sS "http://localhost:8711/export-connection-token?container=agent-a" \
  -H "X-API-KEY: your-key"

# Export a token for Agent B (different container)
curl -sS "http://localhost:8711/export-connection-token?container=agent-b" \
  -H "X-API-KEY: your-key"

# Export a shared container token (for cross-agent collaboration)
curl -sS "http://localhost:8711/export-connection-token?container=shared" \
  -H "X-API-KEY: your-key"
```

Give each token to the corresponding agent. With the [transcendence-memory](https://github.com/leekkk2/transcendence-memory) skill installed, the agent runs `/tm connect <token>` and it's ready.

`/export-connection-token` now returns three layers of onboarding material:

- `token`: backward-compatible base64 connection token for `/tm connect <token>`
- `pairing_auth`: explicit endpoint / api_key / container values for manual pairing
- `agent_onboarding`: exact prompts the AI should show the user before importing, plus the auth facts it should proactively disclose

For AI-assisted setup, do not silently import the token. Surface `agent_onboarding.collect_from_user` first, then tell the user which endpoint, container, and auth mode will be written into the local skill config.

### Local Development

```bash
./scripts/bootstrap_dev.sh
export RAG_API_KEY="your-key"
export EMBEDDING_API_KEY="your-key"
./scripts/run_task_rag_server.sh
```

## API Overview

### Text Memory (Lightweight Path)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check with module status (public) |
| `/search` | POST | Semantic vector search |
| `/embed` | POST | Rebuild LanceDB index |
| `/ingest-memory/objects` | POST | Store typed memory objects |
| `/ingest-structured` | POST | Structured JSON ingest |
| `/containers/{c}/memories/{id}` | PUT/DELETE | Update/delete individual memories |

### Multimodal RAG (Knowledge Graph Path)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/documents/text` | POST | Ingest text into knowledge graph (async — returns a job id) |
| `/documents/file` | POST | Upload a PDF/image/MD/Office file (async — returns a job id) |
| `/documents/upload` | POST | Alias of `/documents/file` |
| `/query` | POST | RAG query with LLM-generated answer |

Knowledge-graph ingestion (`/documents/text`, `/documents/file`,
`/documents/upload`) is **asynchronous**: building the graph takes tens of
seconds to minutes, so the request would otherwise trip edge-proxy timeouts.
Each call stages the input, enqueues a background job, and returns immediately
with a `CommandResponse` whose `pid` field is the job id. Poll
`GET /jobs/{pid}` until `status` is `done` (or `failed`) to learn the outcome.

### Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/containers` | GET | List all containers |
| `/containers/{name}` | DELETE | Delete a container |
| `/export-connection-token` | GET | Export token, manual pairing auth info, and AI onboarding prompts |
| `/jobs/{pid}` | GET | Check async task status |

All endpoints except `/health` require authentication via `X-API-KEY` or `Authorization: Bearer` header.

## Configuration

All settings via `.env` file (see [.env.example](.env.example)):

| Variable | Required | Tier | Description |
|----------|----------|------|-------------|
| `RAG_API_KEY` | Yes | All | API authentication key |
| `EMBEDDING_API_KEY` | Yes | All | Embedding model API key |
| `EMBEDDING_BASE_URL` | No | All | Embedding endpoint (default: OpenAI) |
| `EMBEDDING_MODEL` | No | All | Model name (default: gemini-embedding-001) |
| `LLM_API_KEY` | No | lightrag+ | LLM API key for knowledge graph |
| `LLM_MODEL` | No | lightrag+ | LLM model (default: gemini-2.5-flash) |
| `VLM_API_KEY` | No | everything | Vision model API key |
| `VLM_MODEL` | No | everything | Vision model (default: qwen3-vl-plus) |

## CLI

```bash
pip install -e .
tm-server start              # Start server (default 0.0.0.0:8711)
tm-server start --port 9000  # Custom port
tm-server health             # Health check
tm-server export-token       # Export connection token
```

## Client Skill

Pair with [transcendence-memory](https://github.com/leekkk2/transcendence-memory) — an agent skill that provides built-in commands (`/tm connect`, `/tm search`, `/tm remember`, `/tm query`) for Claude Code, OpenClaw, Codex CLI, and other AI coding agents.

## Rclone Archive → Searchable Memory Workflow

If you have historical archive data sitting at an rclone-mirrored path on the host
and want it searchable through `transcendence-memory-server` without copying the
files into the server's volumes, use this pattern. Replace `<ARCHIVE_ROOT>` (host
path) and `<CONTAINER>` (your container name) with your own values.

1. Keep source data in place at `<ARCHIVE_ROOT>` — for example `/mnt/rclone/my-archive`.
2. Bind-mount the rclone root into the container as read-only with mount propagation.
   Do this in a host-specific `docker-compose.override.yml` (auto-loaded by
   `docker compose`, gitignored by this repo) so upstream defaults stay untouched:
   ```yaml
   # docker-compose.override.yml (host-only, never committed)
   services:
     rag-server:
       volumes:
         - <ARCHIVE_ROOT>:/mnt/archive/source:ro,slave
   ```
3. Expose a canonical in-container source path:
   ```bash
   ln -s /mnt/archive/source /data/tasks/rag/containers/<CONTAINER>/sources/archive
   ```
4. Materialize retrievable objects into canonical storage (`memory_objects.jsonl`):
   ```bash
   python3 scripts/sync_rclone_archive_to_memory_objects.py \
     --origin-root /mnt/archive/source \
     --memory-objects /data/tasks/rag/containers/<CONTAINER>/memory_objects.jsonl
   ```
5. Rebuild LanceDB:
   ```bash
   curl -sS -X POST http://127.0.0.1:8711/embed \
     -H "X-API-KEY: $RAG_API_KEY" -H "Content-Type: application/json" \
     -d '{"container":"<CONTAINER>","wait":true}'
   ```

Why this pattern is recommended:
- original archive path stays unchanged
- container access remains read-only and auditable
- retrieval still goes through the server's canonical `memory_objects.jsonl -> /embed -> LanceDB` path
- avoids treating raw FUSE/rclone directories as a live database

For production hosts, prefer the host-side `rclone-sync.timer` pattern (see
[Docker Deployment](docs/deployment/docker-deployment.md#rclone-integration-without-the-deadlock-risk))
so an unhealthy FUSE mount never blocks container reads.

## Auto-Deploy on Tag (GitHub Actions)

Pushing a `v*.*.*` tag builds and publishes the image, then SSHes to your
server and rolls it forward. Zero manual steps after the tag.

The deploy workflow (`.github/workflows/deploy.yml`) is **opt-in by secret**:
forks without `DEPLOY_HOST` / `DEPLOY_SSH_KEY` configured will see it skip
silently. Configure it once with the bundled helper:

```bash
# On your workstation, with gh CLI authenticated to your fork:
bash deploy/configure-github-deploy.sh \
  --host    your.host.example.com \
  --user    ubuntu \
  --port    22 \
  --path    /opt/transcendence-memory-server \
  --sudo    sudo
```

The helper:

1. Generates a dedicated `ed25519` deploy key (separate from your personal key) under `~/.ssh/transcendence-memory-deploy/`.
2. Pins your host's SSH fingerprint via `ssh-keyscan`.
3. Writes the required GitHub Secrets (`DEPLOY_HOST`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`) and Variables (`DEPLOY_USER`, `DEPLOY_PORT`, `DEPLOY_PATH`, `DEPLOY_SUDO`, `DEPLOY_SMOKE`) via `gh`.
4. Prints the one command you must run on your workstation to authorize the new key on the host (and a sudoers snippet for passwordless `docker` + `systemctl reload rag-everything`).

After that, every successful tag-push CI/CD run triggers the deploy automatically.
Manual redeploy from the Actions tab:

```bash
gh workflow run deploy.yml -f ref=v0.6.0
```

**Security posture**: the deploy key is repo-scoped, the workflow runs the
remote script over a single SSH connection (no third-party action), and the
host fingerprint is pinned so a forged DNS / MITM attempt fails the connection
instead of silently re-trusting. The full design is in [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).

## Documentation

- [Quick Start](docs/deployment/quickstart.md)
- [Docker Deployment](docs/deployment/docker-deployment.md)
- [Reverse Proxy](docs/deployment/reverse-proxy.md)
- [Environment Reference](docs/deployment/environment-reference.md)
- [API Contract](docs/api-contract.md)
- [Health Check](docs/operations/health-check.md)
- [Troubleshooting](docs/operations/troubleshooting.md)
- [Development Bootstrap](docs/development-bootstrap.md)
- [Auto-deploy workflow](.github/workflows/deploy.yml)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Pull requests welcome.

## License

[MIT](LICENSE)
