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

1. 导出 `RAG_API_KEY`、`EMBEDDING_API_KEY`
2. 将 `WORKSPACE` 指向当前仓库根目录
3. 准备运行时目录：`tasks/active`、`tasks/archived`、`tasks/rag/containers/imac`、`memory`、`memory_archive`
4. 安装 `fastapi`、`uvicorn`、`requests`、`numpy`、`lancedb`、`pyarrow`
5. 运行：

```bash
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## Runtime Auth

- Header: `X-API-KEY: <RAG_API_KEY>`
- 或 `Authorization: Bearer <RAG_API_KEY>`

## Runtime Notes

- 默认端口：`8711`
- 当前生产目标机器：Eva
- 现有 HTTPS 入口：`https://rag.zweiteng.tk`
- 当前主链统一为 `LanceDB-only`
- `POST /build-manifest` 仅保留为 deprecated no-op，用于明确旧阶段已经退出主链

## Docs

- [docs/README.md](docs/README.md)
- [docs/server-boundary.md](docs/server-boundary.md)
- [docs/api-contract.md](docs/api-contract.md)
- [docs/development-bootstrap.md](docs/development-bootstrap.md)
- [docs/evolution/README.md](docs/evolution/README.md)
