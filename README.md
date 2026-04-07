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
| `/documents/text` | POST | Ingest text into knowledge graph |
| `/documents/upload` | POST | Upload PDF/image/MD files |
| `/query` | POST | RAG query with LLM-generated answer |

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

## Documentation

- [Quick Start](docs/deployment/quickstart.md)
- [Docker Deployment](docs/deployment/docker-deployment.md)
- [Reverse Proxy](docs/deployment/reverse-proxy.md)
- [Environment Reference](docs/deployment/environment-reference.md)
- [API Contract](docs/api-contract.md)
- [Health Check](docs/operations/health-check.md)
- [Troubleshooting](docs/operations/troubleshooting.md)
- [Development Bootstrap](docs/development-bootstrap.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Pull requests welcome.

## License

[MIT](LICENSE)
