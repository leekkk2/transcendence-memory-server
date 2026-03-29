# Development Bootstrap

## 目标

让当前仓库以最小代价进入“可本地启动、可解释、可继续演进”的 server 开发状态。

## 前置依赖

- Python 3.11+
- `fastapi`
- `uvicorn`
- `requests`
- `numpy`
- `faiss-cpu`

示例安装：

```bash
python3 -m pip install fastapi uvicorn requests numpy faiss-cpu
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
curl -sS \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac"}' \
  http://127.0.0.1:8711/build-manifest
```

## 当前已知约束

- `embed` 依赖 `faiss-cpu`
- 端点返回的是脚本执行包装结果，不是正式业务响应模型
- 目录名与脚本名还保留 `task_rag_*` 历史语义，本轮不做重命名
- 如果后续要把 repo 真正升级为平台化 server，应优先统一 runtime layout 与 schema

## 本地闭环 smoke test

如果只想验证“client ingest 对象在 build-manifest + embed 后可被 search 命中”的最小闭环，可运行：

```bash
python3 scripts/smoke_test_client_ingest_search.py
```

该脚本会：
- 创建临时 `WORKSPACE`
- 通过 `POST /ingest-memory/objects` 写入 client objects
- 调用 `/build-manifest`、`/embed`、`/search`
- 通过 monkeypatch 本地 embedding 函数避免依赖外部 embedding 服务

它验证的是服务闭环与数据流，不是外部 embedding provider 联通性。

## wrapper 路径 smoke test

如果想额外验证当前 FastAPI wrapper 风格端点的子进程路径仍能闭环，可运行：

```bash
python3 scripts/smoke_test_client_ingest_wrapper_flow.py
```

该脚本会：
- 创建临时 `WORKSPACE`
- 通过 `POST /ingest-memory/objects` 写入 client objects
- 先用真实 `task_rag_build_manifest.py` 生成 `manifest.jsonl`
- 再通过 FastAPI wrapper 调用真实 `task_rag_build_manifest.py` 与临时 fake `task_rag_embed.py` / `task_rag_search.py`
- 验证 wrapper `stdout` 返回的搜索结果中，client-ingested `obj-alpha` 能作为 top hit 返回

它验证的是：即使当前服务仍是 script-wrapper 语义，client ingest → build-manifest → embed → search 这条 wrapper 路径也具备最小可证明闭环。
