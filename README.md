# transcendence-memory-server

> 本地目录仍为 `rag-everything/`，但当前私有服务端仓库的命名上下文已经切换为 `transcendence-memory-server`。本次 bootstrap 不主动改本地目录名。

`transcendence-memory-server` 是 Transcendence Memory 体系里的私有 server 实现入口。当前仓库仍以轻量 FastAPI + 脚本编排为主，负责 memory ingestion、manifest build、embedding、search 与私有部署接入。

## 当前职责

- 暴露私有 HTTP API
- 编排 manifest 构建、memory 引用摄取、embedding 和 search
- 维护服务端运行约束与私有部署入口
- 作为后续 server-side implementation 的收口仓库

当前不负责：

- agent / client enhancer 适配
- workspace 级规划、handoff 与多项目编排
- 未来开源抽象层的稳定公共接口承诺

详见 [docs/server-boundary.md](docs/server-boundary.md)。

## 当前实现概览

- Server entry: `scripts/task_rag_server.py`
- Search pipeline: `scripts/task_rag_search.py`
- Manifest builder: `scripts/task_rag_build_manifest.py`
- Embedding pipeline: `scripts/task_rag_embed.py`
- Memory ref ingestion: `scripts/task_rag_ingest_memory_refs.py`
- Nginx reference: `docs/nginx-rag.zweiteng.tk.conf`

## HTTP API

当前实现暴露以下端点：

- `POST /search`
- `POST /build-manifest`
- `POST /ingest-memory`
- `POST /embed`

请求/响应与 memory chunk 草案见：

- [docs/api-contract.md](docs/api-contract.md)

## Development Bootstrap

完整说明见 [docs/development-bootstrap.md](docs/development-bootstrap.md)。

最短启动路径：

1. 导出 `RAG_API_KEY`、`EMBEDDING_API_KEY`
2. 将 `WORKSPACE` 指向当前仓库根目录
3. 准备运行时目录：`tasks/active`、`tasks/archived`、`tasks/rag/containers/imac`、`memory`、`memory_archive`
4. 安装 `fastapi`、`uvicorn`、`requests`、`numpy`、`faiss-cpu`
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
- 当前实现仍保留历史 `task_rag_*` 命名与 `tasks/rag/...` 存储路径；后续可以再做统一重命名，本次不处理

## Docs

- [docs/README.md](docs/README.md)
- [docs/server-boundary.md](docs/server-boundary.md)
- [docs/api-contract.md](docs/api-contract.md)
- [docs/development-bootstrap.md](docs/development-bootstrap.md)
