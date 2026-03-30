# Development Bootstrap

## 目标

让当前仓库以最小代价进入“可本地启动、可解释、可继续演进”的 `LanceDB-only` server 开发状态。

## 前置依赖

- Python 3.11+
- `fastapi`
- `uvicorn`
- `requests`
- `numpy`
- `lancedb`
- `pyarrow`

示例安装：

```bash
python3 -m pip install fastapi uvicorn requests numpy lancedb pyarrow
```

## 必要环境变量

```bash
export WORKSPACE="$PWD"
export RAG_API_KEY="replace-me"
export EMBEDDING_API_KEY="replace-me"
export EMBEDDING_MODEL="gemini-embedding-001"
export EMBEDDINGS_BASE_URL="https://newapi.zweiteng.tk/v1"
export GOOGLE_EMBEDDING_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models"
```

## 运行时目录

当前脚本仍使用历史 `tasks/...` 目录约定，而不是仓库里的 `tasks_rag/` 名称。
本地最小目录可以先这样准备：

```bash
mkdir -p \
  tasks/active \
  tasks/archived \
  tasks/rag/containers/imac \
  memory \
  memory_archive
```

## 启动服务

```bash
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## 最小 smoke test

```bash
curl -sS http://127.0.0.1:8711/health
```

## Typed object proof smoke

如果只想验证“typed client objects 能被稳定写入 canonical storage”，可运行：

```bash
python3 scripts/smoke_test_client_ingest_search.py
```

该脚本会：

- 创建临时 `WORKSPACE`
- 通过 `POST /ingest-memory/objects` 写入 typed objects
- 直接检查 `memory_objects.jsonl`

## Pytest verification

如果要验证真正的 `LanceDB-only` 检索链路，可运行：

```bash
python3 -m pytest tests/test_task_rag_server_memory_objects.py -q
```

该测试会：

- 创建临时 `WORKSPACE`
- 启动本地 fake embedding HTTP 服务
- 通过 `POST /ingest-memory/objects` 写入 typed objects
- 调用 `POST /embed` 重建 LanceDB
- 调用 `POST /search` 验证对象可被命中

## 当前已知约束

- `embed` 和 `search` 依赖 `lancedb` 与 `pyarrow`
- 当前服务端仍保留 script-wrapper observability 字段，但主链架构已经统一为 `LanceDB-only`
- `POST /build-manifest` 已退出主链，仅保留 deprecated no-op
- 目录名与脚本名仍保留 `task_rag_*` 历史语义，本轮不做重命名
