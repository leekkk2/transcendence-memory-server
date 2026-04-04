# Transcendence Memory Server

自托管的多模态 RAG 记忆服务。为 AI agent 提供集中式云端存储、记忆检索和知识图谱能力。

## 特性

- **文本记忆 CRUD** — 增删改查，支持 typed object 与结构化写入
- **多模态 RAG** — PDF / 图片 / 表格文档理解（基于 RAG-Anything）
- **知识图谱** — LightRAG 驱动的实体关系抽取与混合检索
- **容器隔离** — 多 agent / 多项目独立存储空间
- **Connection Token** — 一键生成鉴权令牌，客户端即插即用
- **LanceDB 向量存储** — 零外部依赖的嵌入式向量数据库

## 快速部署

### Docker（推荐）

```bash
cp .env.example .env
# 编辑 .env，填入 API Key
docker compose up -d
curl http://localhost:8711/health
```

### pip

```bash
pip install -e "."
# 设置环境变量（或创建 .env）
export WORKSPACE="$PWD" RAG_API_KEY="your-key" EMBEDDING_API_KEY="your-key"
mkdir -p tasks/active tasks/archived tasks/rag/containers memory memory_archive
tm-server start
```

### 从源码运行

```bash
./scripts/bootstrap_dev.sh
export WORKSPACE="$PWD" RAG_API_KEY="your-key" EMBEDDING_API_KEY="your-key"
mkdir -p tasks/active tasks/archived tasks/rag/containers memory memory_archive
./scripts/run_task_rag_server.sh
```

## CLI

```bash
tm-server start              # 启动服务（默认 0.0.0.0:8711）
tm-server start --port 9000  # 自定义端口
tm-server health             # 健康检查
tm-server export-token       # 导出 connection token
```

## 配套客户端技能

```bash
# Claude Code
claude mcp add transcendence-memory
```

客户端仓库：[transcendence-memory](https://zweiteng.tk/server/transcendence-memory)

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（匿名） |
| `/search` | POST | 语义检索 |
| `/embed` | POST | 向量索引重建 |
| `/ingest-memory` | POST | LanceDB 记忆写入 |
| `/ingest-memory/objects` | POST | Typed object 写入 |
| `/ingest-structured` | POST | 结构化 JSON 写入 |
| `/export-connection-token` | GET | 导出连接令牌 |

认证方式：`X-API-KEY: <RAG_API_KEY>` 或 `Authorization: Bearer <RAG_API_KEY>`

详见 [API Contract](docs/api-contract.md)。

## 配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `RAG_API_KEY` | 服务端 API 密钥 | （必填） |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | `https://newapi.zweiteng.tk/v1` |
| `EMBEDDING_API_KEY` | Embedding API 密钥 | （必填） |
| `EMBEDDING_MODEL` | Embedding 模型名 | `gemini-embedding-001` |
| `EMBEDDING_DIM` | 向量维度 | `3072` |
| `LLM_MODEL` | 知识图谱构建用 LLM | `gemini-2.5-flash` |
| `LLM_BASE_URL` | LLM API 地址 | `https://newapi.zweiteng.tk/v1` |
| `LLM_API_KEY` | LLM API 密钥 | （必填） |
| `VLM_MODEL` | 视觉语言模型 | `qwen3-vl-plus` |
| `RAG_ADVERTISED_ENDPOINT` | 对外 endpoint | `http://localhost:8711` |
| `WORKSPACE` | 数据存储目录 | `/data`（Docker） |

完整说明见 [.env.example](.env.example) 和 [环境变量参考](docs/deployment/environment-reference.md)。

## 文档

| 主题 | 链接 |
|------|------|
| 快速部署 | [docs/deployment/quickstart.md](docs/deployment/quickstart.md) |
| Docker 部署 | [docs/deployment/docker-deployment.md](docs/deployment/docker-deployment.md) |
| systemd 部署 | [docs/deployment/systemd-deployment.md](docs/deployment/systemd-deployment.md) |
| 反向代理 | [docs/deployment/reverse-proxy.md](docs/deployment/reverse-proxy.md) |
| 健康检查 | [docs/operations/health-check.md](docs/operations/health-check.md) |
| 备份恢复 | [docs/operations/backup-restore.md](docs/operations/backup-restore.md) |
| 开发 Bootstrap | [docs/development-bootstrap.md](docs/development-bootstrap.md) |
| Server Boundary | [docs/server-boundary.md](docs/server-boundary.md) |

## License

MIT
