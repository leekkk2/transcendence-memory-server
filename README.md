# 🧠 Transcendence Memory Server

> **The Persistent Substrate for Multi-Agent Intelligence.**

Transcendence Memory Server 是一个自托管的、为 AI Agent 量身定制的 **记忆中心 (Memory Cloud)**。它利用 RAG (检索增强生成) 技术，为长期运行的 Agent 提供跨会话的任务状态保存、决策跟踪和知识检索能力。

---

## 🚀 核心架构

- **RAG 引擎**: 基于 **LanceDB** 的 server-side 向量检索，支持秒级增量索引。
- **存储对象**: 支持 Task Cards (任务卡)、Markdown 记忆片段、Typed Objects (强类型对象) 以及任意结构化 JSON。
- **多租户隔离**: 通过 `Container` 机制实现不同项目或 Agent 空间的物理隔离。
- **知识图谱 (Alpha)**: 集成 LightRAG，支持实体关系抽取与深度关联检索。
- **一键连接**: 独特的 Connection Token 机制，让客户端技能 [transcendence-memory](../transcendence-memory/README.md) 能够即插即用。

---

## 🛠️ 快速部署

### Docker (推荐)

```bash
cp .env.example .env
# 编辑 .env，填入 API Key 和 Endpoint
docker compose up -d
curl http://localhost:8711/health
```

### 本地运行

```bash
./scripts/bootstrap_dev.sh
# 导出环境变量
export RAG_API_KEY="your-key" 
export EMBEDDING_API_KEY="your-key"
# 启动
./scripts/run_task_rag_server.sh
```

---

## 💻 CLI 指令

```bash
tm-server start              # 启动服务 (默认 0.0.0.0:8711)
tm-server start --port 9000  # 自定义端口
tm-server health             # 健康检查
tm-server export-token       # 导出 connection token
```

---

## 📡 API 概览

| 端点 | 方法 | 说明 |
| :--- | :--- | :--- |
| `/health` | GET | 健康检查 (匿名) |
| `/search` | POST | 语义检索 |
| `/embed` | POST | 向量索引重建 |
| `/ingest-memory` | POST | LanceDB 记忆写入 |
| `/ingest-memory/objects` | POST | Typed object 写入 |
| `/ingest-structured` | POST | 结构化 JSON 写入 |
| `/export-connection-token` | GET | 导出连接令牌 |

---

## ⚙️ 配置说明 (Environment Variables)

所有配置均建议通过 `.env` 文件或环境变量注入，系统不再预设特定私有域名的默认值。

| 环境变量 | 说明 | 示例/建议值 |
| :--- | :--- | :--- |
| `RAG_API_KEY` | 服务端鉴权密钥 | (必填) |
| `EMBEDDING_BASE_URL` | Embedding API 地址 | `https://api.openai.com/v1` |
| `EMBEDDING_API_KEY` | Embedding API 密钥 | (必填) |
| `EMBEDDING_MODEL` | Embedding 模型名 | `text-embedding-3-small` |
| `LLM_BASE_URL` | LLM API 地址 | `https://api.openai.com/v1` |
| `LLM_API_KEY` | LLM API 密钥 | (必填) |
| `WORKSPACE` | 数据存储根目录 | `./data` |

---

## 🔗 配套生态

本服务端作为“记忆大脑”，需要配合以下客户端技能来实现完整的 Agent 增强：

- **核心技能端**: [transcendence-memory](../transcendence-memory/README.md) —— 提供 AI Agent 使用的自然语言指令集。

---

## 📖 文档索引

- [API 契约](docs/api-contract.md)
- [部署指南 (Docker/Systemd)](docs/deployment/docker-deployment.md)
- [演进历史 (RAG Evolution)](docs/evolution/README.md)
- [开发者快速入门](docs/development-bootstrap.md)

---

## License
MIT
