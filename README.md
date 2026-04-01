# transcendence-memory-server

`transcendence-memory-server` 是 Transcendence Memory 体系里的私有 server 实现入口，负责统一的服务端 ingest、storage、retrieval 与私有部署接入。

## 当前职责

- 暴露私有 HTTP API
- 统一用 LanceDB 承载服务端检索主链
- 重建 task cards、markdown memory、typed client objects 的服务端索引
- 提供结构化 JSON-like 数据入库能力
- 维护服务端运行约束与私有部署入口

当前不负责：

- agent / client enhancer 适配
- workspace 级规划、handoff 与多项目编排
- 未来开源抽象层的稳定公共接口承诺

详见 [docs/server-boundary.md](docs/server-boundary.md)。

## 当前实现概览

- Server entry: `scripts/task_rag_server.py`
- Shared runtime: `scripts/task_rag_runtime.py`
- Canonical LanceDB ingest: `scripts/task_rag_lancedb_ingest.py`
- Search pipeline: `scripts/task_rag_search.py`
- Structured ingest: `scripts/task_rag_structured_ingest.py`
- Ops helpers: `scripts/load_rag_config.sh`, `scripts/run_task_rag_server.sh`
  - `load_rag_config.sh` 从 `~/.openclaw/workspace/tools/rag-config.json`（或 `RAG_CONFIG_FILE` 覆盖路径）导出 `RAG_ENDPOINT`、`RAG_AUTH_HEADER`、`RAG_API_KEY`、`RAG_DEFAULT_CONTAINER`
  - `run_task_rag_server.sh` 默认切到当前 server 仓库根目录执行，并将 `WORKSPACE` 默认指向该仓库根；只有在明确需要 monorepo 根级运行时才覆盖 `WORKSPACE`
- Nginx reference: `docs/nginx-rag.zweiteng.tk.conf`

## HTTP API

当前实现暴露以下端点：

- `GET /health`
- `POST /search`
- `POST /embed`
- `POST /ingest-memory`
- `GET /ingest-memory/contract`
- `POST /ingest-memory/objects`
- `POST /ingest-structured`
- `POST /build-manifest`（deprecated no-op）

请求/响应草案见 [docs/api-contract.md](docs/api-contract.md)。

## Development Bootstrap

完整说明见 [docs/development-bootstrap.md](docs/development-bootstrap.md)。

最短启动路径：

1. 将 `WORKSPACE` 指向当前仓库根目录（当前文档默认在 server 子仓库目录内执行，因此通常是 `export WORKSPACE="$PWD"`）
2. 导出 `RAG_API_KEY`、`EMBEDDING_API_KEY`
3. 如需自定义 embedding provider endpoint，当前 runtime **优先读取** `EMBEDDING_BASE_URL`，同时兼容 `EMBEDDINGS_BASE_URL`；为避免歧义，建议本地调试时两个变量同时设置为同一值
4. 准备运行时目录：`tasks/active`、`tasks/archived`、`tasks/rag/containers/imac`、`memory`、`memory_archive`
5. 运行项目内 bootstrap，准备 `.venv-task-rag-server` 与最小开发依赖（含 `pytest`、`httpx`，默认优先使用 `python3.11`）：

```bash
./scripts/bootstrap_dev.sh
```

6. 运行：

```bash
./scripts/run_task_rag_server.sh
```

如需先加载私有 RAG endpoint / auth 配置，再启动 server，可在同一 shell 中串联：

```bash
source ./scripts/load_rag_config.sh
./scripts/run_task_rag_server.sh
```

等价手动命令：

```bash
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## Runtime Auth

- Header: `X-API-KEY: <RAG_API_KEY>`
- 或 `Authorization: Bearer <RAG_API_KEY>`

## Runtime Notes

- 默认端口：`8711`
- `scripts/run_task_rag_server.sh` 当前默认在 `transcendence-memory-server/` 仓库根目录内启动 uvicorn；如需让 runtime 数据落在 monorepo 根级 `tasks/rag/...`，需显式覆盖 `WORKSPACE=/path/to/skills-workspace`
- 私有部署链路当前口径：`rag.zweiteng.tk` → Nginx (`docs/nginx-rag.zweiteng.tk.conf`) → `127.0.0.1:8711` → `./scripts/run_task_rag_server.sh`
- 当前生产目标机器：Eva
- 现有 HTTPS 入口：`https://rag.zweiteng.tk`
- 当前主链统一为 `LanceDB-only`
- embedding provider endpoint 当前 runtime 解析顺序为：`EMBEDDING_BASE_URL` → `EMBEDDINGS_BASE_URL` → 默认值；因此文档与调用方都应显式意识到 legacy alias 目前具有更高优先级
- `POST /build-manifest` 仅保留为 deprecated no-op，用于明确旧阶段已经退出主链

## Docs

- [docs/README.md](docs/README.md)
- [docs/server-boundary.md](docs/server-boundary.md)
- [docs/api-contract.md](docs/api-contract.md)
- [docs/development-bootstrap.md](docs/development-bootstrap.md)
- [docs/evolution/README.md](docs/evolution/README.md)
