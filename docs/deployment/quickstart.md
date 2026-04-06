# 部署快速入门 / Deployment Quickstart

## 身份优先

本页面向 **backend 身份** 或 **both 身份中的 backend 阶段**。

- 如果当前机器是 `frontend`，请参考 transcendence-memory skill 的客户端文档
- 先检查本地 `operator-identity.md`
- 若身份文档缺失，先补录身份再继续部署

## 前置条件

```bash
python3 --version    # >= 3.11
docker --version     # 可选，Docker 部署时需要
docker compose version
```

确认：
- Python 满足仓库要求（当前为 `>=3.11`）
- 若使用 Docker 部署，Docker / Docker Compose 可用
- 网络/代理路径可用（拉镜像或外部依赖时需要）
- 当前会话是否能直接访问宿主机 Docker daemon

## 最短启动路径（裸机）

```bash
cd transcendence-memory-server

# 1. bootstrap 开发环境
./scripts/bootstrap_dev.sh

# 2. 设置环境变量
export WORKSPACE="$PWD"
export RAG_API_KEY="replace-me"
export EMBEDDING_API_KEY="replace-me"
export EMBEDDING_BASE_URL="https://your-embedding-endpoint/v1"
export EMBEDDINGS_BASE_URL="https://your-embedding-endpoint/v1"

# 3. 准备运行时目录
mkdir -p tasks/active tasks/archived tasks/rag/containers/imac memory memory_archive

# 4. 启动服务
./scripts/run_task_rag_server.sh
# 或手动：
# uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711

# 5. 健康检查
curl -sS http://127.0.0.1:8711/health
```

## Docker 部署

详见 [docker-deployment.md](docker-deployment.md)。

默认构建规格为 `lite`。如果下一步就是多模态解析或 `rag-everything` 链路，请在启动前显式设置：

```bash
BUILD_TARGET=full docker compose up -d --build
```

## 反向代理

详见 [reverse-proxy.md](reverse-proxy.md)。

## 环境变量

完整参考见 [environment-reference.md](environment-reference.md)。

## 当前 Runtime 口径

- 默认端口：`8711`
- 默认构建规格：**lite**
- 运行时架构：按 key + 包可用性动态检测
- 认证方式：`X-API-KEY` header 或 `Authorization: Bearer`

## 部署后必须交给前端的信息

后端部署完成后，不能只给前端一个 URL，至少应同时交付：

1. `bundle.json`（由 `backend export-connection` 生成）
2. 当前前端应使用的鉴权模式
3. 前端仍需本地补齐的鉴权材料
4. 前端下一步应执行的命令顺序

如果走 server 原生 `/export-connection-token` 流程，优先把响应里的 `pairing_auth` 与 `agent_onboarding` 一并交给接入方 AI，而不是只转发一个 token。

```bash
transcendence-memory backend export-connection --topology split_machine --output bundle.json
```

## Backend Acceptance

至少确认：
- `GET /health` → 200
- `POST /search` → 200 + 真实结果
- `POST /embed` → 200 + success
- 如目标链路依赖 typed ingest，再验证 `/ingest-memory/objects`

## 排障入口

优先排查：
1. 当前说明是否仍与 canonical backend runtime 一致
2. 环境是否满足 Python / Docker / 网络等前置条件
3. 当前会话是否能访问 Docker daemon
4. advertised endpoint 是否正确
5. handoff / auth / smoke 路径是否闭合

详见 [troubleshooting.md](../operations/troubleshooting.md)。
