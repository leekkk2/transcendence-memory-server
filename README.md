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

## 🛠️ 快速开始

### 1. 启动服务 (Docker)
```bash
cp .env.example .env  # 填入你的 LLM/Embedding API Key
docker-compose up -d
```

### 2. 生成连接令牌
```bash
tm-server export-token
```
*复制输出的 Token，用于在 [transcendence-memory](../transcendence-memory/README.md) 中进行初始化配置。*

---

## 📡 API 概览

| 端点 | 说明 | 核心用途 |
| :--- | :--- | :--- |
| `POST /search` | 语义检索 | Agent 查找历史背景、决策依据 |
| `POST /embed` | 索引重建 | 将最新 Task/Docs 同步至向量库 |
| `POST /ingest-memory/objects` | 强类型写入 | 存储结构化、可追溯的记忆对象 |
| `POST /ingest-structured` | 结构化导入 | 批量导入 JSON/知识库数据 |

*详见 [API Contract](docs/api-contract.md)*。

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
