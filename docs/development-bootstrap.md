# Development Bootstrap

## 目标

让当前仓库以最小代价进入“可本地启动、可解释、可继续演进”的 `LanceDB-only` server 开发状态。

## 前置依赖

- Python 3.11+（当前测试文件使用 `dict[str, str] | None` 等 3.10+ 语法，建议直接使用 3.11）
- `pytest`
- `httpx`
- `fastapi`
- `uvicorn`
- `requests`
- `numpy`
- `lancedb`
- `pyarrow`

推荐先跑项目内 bootstrap：

```bash
./scripts/bootstrap_dev.sh
```

如本机默认 `python3` 仍是 3.9，可显式指定：

```bash
PYTHON_BIN=python3.11 ./scripts/bootstrap_dev.sh
```

它会：

- 创建/复用 `.venv-task-rag-server`
- 默认优先使用 `python3.11`（可通过 `PYTHON_BIN` 覆盖）
- 安装 `pytest` 与当前最小开发依赖
- 让 `scripts/run_task_rag_server.sh` 与 `python -m pytest ...` 共用同一虚拟环境

如果只想手动安装，等价命令为：

```bash
python3 -m pip install pytest httpx fastapi uvicorn requests numpy lancedb pyarrow
```

## 必要环境变量

当前文档默认你就在 `transcendence-memory-server/` 仓库根目录内执行命令，因此：

```bash
export WORKSPACE="$PWD"
export RAG_API_KEY="replace-me"
export EMBEDDING_API_KEY="replace-me"
export EMBEDDING_MODEL="gemini-embedding-001"
export EMBEDDING_BASE_URL="https://newapi.zweiteng.tk/v1"    # runtime 当前优先读取
export EMBEDDINGS_BASE_URL="https://newapi.zweiteng.tk/v1"   # canonical 名称，建议同时设置保持一致
export GOOGLE_EMBEDDING_BASE_URL="https://generativelanguage.googleapis.com/v1beta/models"
```

> 注意：当前 runtime 的实际解析顺序是 `EMBEDDING_BASE_URL` → `EMBEDDINGS_BASE_URL` → 默认值。
> 如果你的 shell 环境里预先存在旧值，单独设置 `EMBEDDINGS_BASE_URL` 可能不会覆盖它；本地调试时最稳妥做法是两个变量一起显式设置为同一个 endpoint。

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
