# transcendence-memory-server

Transcendence Memory 体系的**私有服务端实现**。负责 ingest、storage、retrieval 与部署。

## Quick Navigation

| 主题 | 文档 |
|------|------|
| **快速部署** | [docs/deployment/quickstart.md](docs/deployment/quickstart.md) |
| **Docker ���署** | [docs/deployment/docker-deployment.md](docs/deployment/docker-deployment.md) |
| **systemd 部署** | [docs/deployment/systemd-deployment.md](docs/deployment/systemd-deployment.md) |
| **反向代理** | [docs/deployment/reverse-proxy.md](docs/deployment/reverse-proxy.md) |
| **环境变量参考** | [docs/deployment/environment-reference.md](docs/deployment/environment-reference.md) |
| **健康检查** | [docs/operations/health-check.md](docs/operations/health-check.md) |
| **服务端排障** | [docs/operations/troubleshooting.md](docs/operations/troubleshooting.md) |
| **备份恢复** | [docs/operations/backup-restore.md](docs/operations/backup-restore.md) |
| **升级迁移** | [docs/operations/upgrade-migration.md](docs/operations/upgrade-migration.md) |
| **API Contract** | [docs/api-contract.md](docs/api-contract.md) |
| **开发 Bootstrap** | [docs/development-bootstrap.md](docs/development-bootstrap.md) |
| **Server Boundary** | [docs/server-boundary.md](docs/server-boundary.md) |

## For LLM Agents

直接抓取部署指南：

```bash
curl -s <raw-url>/docs/deployment/quickstart.md
```

## For Humans

从 [快速部署](docs/deployment/quickstart.md) 开始，按需查阅其他文档。

## Shortest Start

```bash
cd transcendence-memory-server
./scripts/bootstrap_dev.sh
export WORKSPACE="$PWD" RAG_API_KEY="replace-me" EMBEDDING_API_KEY="replace-me"
mkdir -p tasks/active tasks/archived tasks/rag/containers/imac memory memory_archive
./scripts/run_task_rag_server.sh
curl -sS http://127.0.0.1:8711/health
```

## HTTP API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（匿名） |
| `/search` | POST | 检索 |
| `/embed` | POST | 索引重建 |
| `/ingest-memory` | POST | LanceDB 重建 |
| `/ingest-memory/contract` | GET | Ingest 语义边界 |
| `/ingest-memory/objects` | POST | Typed object 写入 |
| `/ingest-structured` | POST | 结构化 JSON ingest |
| `/build-manifest` | POST | Deprecated no-op |

详见 [API Contract](docs/api-contract.md)���

## Authentication

- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

## Architecture

- 主链：**LanceDB-only**
- 默认端口：`8711`
- 详见 [Server Boundary](docs/server-boundary.md)

## This Repo Owns

- 认证 HTTP 端点
- LanceDB ingest 与 retrieval
- Typed object 持久化与结构化 ingest
- 运行时脚本与服务包装
- **所有服务端部署、运维文档**

## This Repo Does Not Own

- 客户端技能包 → `transcendence-memory`
- Workspace 级规划与编排 → `transcendence-memory-workspace`
- Agent/client enhancer → `skills-hub`

## Client Skill

客户端使用与连接请参考 [`transcendence-memory`](https://zweiteng.tk/server/transcendence-memory) 仓库。

## Development

详见 [Development Bootstrap](docs/development-bootstrap.md)。

## Docs Index

- [docs/README.md](docs/README.md)
- [docs/server-boundary.md](docs/server-boundary.md)
- [docs/api-contract.md](docs/api-contract.md)
- [docs/development-bootstrap.md](docs/development-bootstrap.md)
- [docs/deployment/](docs/deployment/) — 部署文档
- [docs/operations/](docs/operations/) — 运维文档
- [docs/identity/](docs/identity/) — 身份文档
- [docs/evolution/](docs/evolution/) — 演进记录
