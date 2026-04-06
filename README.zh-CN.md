# Transcendence Memory Server

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)

> **自托管多模态 RAG 云记忆服务 — 多个 AI Agent 的共享大脑。**

[English](README.md)

Transcendence Memory Server 是一个云端记忆后端，多个 AI Agent 可以同时连接。每个 Agent 在各自隔离的容器中存储自己的记忆，同时可以跨容器查询其他 Agent 的知识 — 将孤立的 AI 会话变成协作式的、持久化的知识网络。

```
  Agent A (Claude Code)          Agent B (Codex CLI)          Agent C (OpenClaw)
       |                              |                              |
       |  存储和检索自己的记忆          |  存储和检索自己的记忆          |  存储和检索自己的记忆
       |  跨容器查询 B, C              |  跨容器查询 A, C              |  跨容器查询 A, B
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

## 为什么需要云记忆？

| 痛点 | 没有 Transcendence | 有 Transcendence |
|------|-------------------|-----------------|
| 会话结束 | 记忆丢失 | 持久化到云端，随时恢复 |
| 切换 Agent | 从零开始 | 新 Agent 通过搜索继承上下文 |
| 跨项目 | 知识孤岛 | Agent B 查询 Agent A 的决策 |
| Agent 团队 | 各自为战 | 共享容器，集体知识库 |
| 新 Agent 上手 | 重新解释一切 | 直接检索历史决策和背景 |

## 特性

- **多 Agent 云记忆** — 一个服务端，多个 Agent 连接；各自存储，互相查询
- **Lite / Full 构建规格** — 默认 `lite` 镜像，可按需切到带多模态依赖的 `full`
- **容器隔离** — 按 Agent 或按项目的命名空间，支持完整 CRUD；共享容器用于团队知识
- **LanceDB 向量检索** — 亚秒级语义搜索，覆盖任务卡、记忆对象和结构化数据
- **LightRAG 知识图谱** — 实体/关系抽取，支持混合检索（本地 + 全局 + 关键词）
- **RAG-Anything 多模态** — PDF、图片、表格解析，支持视觉模型
- **架构自动检测** — 根据已配置的 API key 自动启用对应能力
- **连接令牌** — 给每个 Agent 一个 token，一步连接
- **零权限问题** — Docker named volume，无需处理宿主机目录权限

## 构建规格

当前服务支持两种构建规格：

| 规格 | 默认 | 包含能力 |
|------|------|---------|
| `lite` | 是 | FastAPI、LanceDB、LightRAG、typed ingest、connection token 导出 |
| `full` | 否 | `lite` 全部能力 + `raganything` 多模态依赖 |

切换方式：

```bash
# 默认 lite
docker compose up -d --build

# full 多模态构建
BUILD_TARGET=full docker compose up -d --build
```

`/health` 会显式返回 `build_flavor`、`multimodal_capable` 和 `degraded_reasons`。

## 平台支持

- **Python 包** — CI 会在 `Linux`、`macOS`、`Windows` 上，对 Python `3.11`、`3.12`、`3.13` 进行安装与测试验证
- **Docker 镜像** — 发布 `linux/amd64` 与 `linux/arm64`
- **macOS / Windows 宿主机** — 通过 Docker Desktop 运行 Linux 容器来支持
- **非 Linux 原生容器** — 本项目不会发布原生 macOS 容器镜像，也不会发布原生 Windows 容器镜像

## 架构层级

服务根据 `.env` 配置自动检测能力层级：

| 层级 | 所需密钥 | 能力 |
|------|---------|------|
| `lancedb-only` | `EMBEDDING_API_KEY` | 向量检索、类型化对象、结构化写入 |
| `lancedb+lightrag` | + `LLM_API_KEY` | + 知识图谱、实体抽取、混合查询 |
| `rag-everything` | + `VLM_API_KEY` | + PDF/图片/表格解析、视觉模型查询 |

## 快速开始

### Docker（推荐）

如果你在 macOS 或 Windows 上使用 Docker Desktop，只要当前运行的是 Linux containers 模式，就可以直接部署本服务。Intel 宿主机通常拉取 `linux/amd64`，Apple Silicon 与 Windows on Arm 可拉取 `linux/arm64`。

```bash
git clone https://github.com/leekkk2/transcendence-memory-server.git
cd transcendence-memory-server
cp .env.example .env    # 编辑填入你的 API key
# 如需完整多模态依赖，可额外设置 BUILD_TARGET=full
docker compose up -d --build
curl http://localhost:8711/health
```

### 生产部署（VPS + Nginx）

```bash
# 预检
bash scripts/preflight_check.sh

# 如需完整多模态依赖，可额外设置 BUILD_TARGET=full
# 部署（端口仅绑定 127.0.0.1，配合 Nginx 反代）
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

### 连接你的 Agent

服务运行后，为每个 Agent 生成独立的连接令牌：

```bash
# 为 Agent A 导出 token
curl -sS "http://localhost:8711/export-connection-token?container=agent-a" \
  -H "X-API-KEY: your-key"

# 为 Agent B 导出 token（不同容器）
curl -sS "http://localhost:8711/export-connection-token?container=agent-b" \
  -H "X-API-KEY: your-key"

# 导出共享容器的 token（用于跨 Agent 协作）
curl -sS "http://localhost:8711/export-connection-token?container=shared" \
  -H "X-API-KEY: your-key"
```

将 token 交给对应的 Agent。安装了 [transcendence-memory](https://github.com/leekkk2/transcendence-memory) 技能后，Agent 执行 `/tm connect <token>` 即可连接。

现在 `/export-connection-token` 会同时返回三层引导材料：

- `token`：兼容现有 `/tm connect <token>` 的 base64 连接令牌
- `pairing_auth`：供手动配对使用的 `endpoint / api_key / container`
- `agent_onboarding`：AI 在安装引导时应先向用户展示的采集提示，以及应主动告知用户的鉴权事实

如果是 AI 辅助安装，不应静默导入 token。应先展示 `agent_onboarding.collect_from_user` 中的问题，再明确告知用户最终会写入本地技能配置的 endpoint、container 和鉴权模式。

### 本地开发

```bash
./scripts/bootstrap_dev.sh
export RAG_API_KEY="your-key"
export EMBEDDING_API_KEY="your-key"
./scripts/run_task_rag_server.sh
```

## API 概览

### 文本记忆（轻量路径）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 + 模块状态（公开） |
| `/search` | POST | 语义向量检索 |
| `/embed` | POST | 重建 LanceDB 索引 |
| `/ingest-memory/objects` | POST | 写入类型化记忆对象 |
| `/ingest-structured` | POST | 结构化 JSON 写入 |
| `/containers/{c}/memories/{id}` | PUT/DELETE | 更新/删除单条记忆 |

### 多模态 RAG（知识图谱路径）

| 端点 | 方法 | 说明 |
|------|------|------|
| `/documents/text` | POST | 文本入知识图谱 |
| `/documents/upload` | POST | 上传 PDF/图片/MD 文件 |
| `/query` | POST | RAG 查询（LLM 生成答案） |

### 管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/containers` | GET | 列出所有容器 |
| `/containers/{name}` | DELETE | 删除容器 |
| `/export-connection-token` | GET | 导出 token、手动配对鉴权材料与 AI 安装引导提示 |
| `/jobs/{pid}` | GET | 查询异步任务状态 |

除 `/health` 外，所有端点均需通过 `X-API-KEY` 或 `Authorization: Bearer` 头认证。

## 配置

所有配置通过 `.env` 文件设置（参见 [.env.example](.env.example)）：

| 变量 | 必需 | 层级 | 说明 |
|------|------|------|------|
| `RAG_API_KEY` | 是 | 全部 | API 认证密钥 |
| `EMBEDDING_API_KEY` | 是 | 全部 | Embedding 模型密钥 |
| `EMBEDDING_BASE_URL` | 否 | 全部 | Embedding 服务地址（默认 OpenAI） |
| `EMBEDDING_MODEL` | 否 | 全部 | 模型名（默认 gemini-embedding-001） |
| `LLM_API_KEY` | 否 | lightrag+ | LLM 密钥（知识图谱） |
| `LLM_MODEL` | 否 | lightrag+ | LLM 模型（默认 gemini-2.5-flash） |
| `VLM_API_KEY` | 否 | everything | 视觉模型密钥 |
| `VLM_MODEL` | 否 | everything | 视觉模型（默认 qwen3-vl-plus） |

## CLI

```bash
pip install -e .
tm-server start              # 启动服务（默认 0.0.0.0:8711）
tm-server start --port 9000  # 自定义端口
tm-server health             # 健康检查
tm-server export-token       # 导出连接令牌
```

## 客户端技能

搭配 [transcendence-memory](https://github.com/leekkk2/transcendence-memory) 使用 — 一个 Agent 技能插件，提供内置命令（`/tm connect`、`/tm search`、`/tm remember`、`/tm query`），兼容 Claude Code、OpenClaw、Codex CLI 等 AI 编程助手。

## 文档

- [快速入门](docs/deployment/quickstart.md)
- [Docker 部署](docs/deployment/docker-deployment.md)
- [反向代理](docs/deployment/reverse-proxy.md)
- [环境变量参考](docs/deployment/environment-reference.md)
- [API 契约](docs/api-contract.md)
- [健康检查](docs/operations/health-check.md)
- [排障指南](docs/operations/troubleshooting.md)
- [开发快速入门](docs/development-bootstrap.md)

## 参与贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。欢迎提交 Pull Request。

## 许可证

[MIT](LICENSE)
