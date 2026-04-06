# API 契约草案

## 状态

草案 — 分支统一后整合的主线契约。

## 目标

为 Transcendence Memory 服务端检索提供唯一的规范私有服务器契约。

## 认证

请求应提供以下任一认证头：

- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

## 运行时假设

当前文档化的运行时假设：

- `WORKSPACE` 指向包含 `tasks/`、`memory/` 和 `memory_archive/` 的活跃运行时根目录
- 受保护端点需要设置 `RAG_API_KEY`
- 基于 embedding 的端点需要设置 `EMBEDDING_API_KEY`
- embedding provider 的 base URL 解析顺序为：先 `EMBEDDING_BASE_URL`，再 `EMBEDDINGS_BASE_URL`，最后使用内置默认值
- 为避免本地调试时的 shell 状态歧义，建议同时将 `EMBEDDING_BASE_URL` 和 `EMBEDDINGS_BASE_URL` 设为相同的值

最小文档化的本地启动路径：

```bash
./scripts/bootstrap_dev.sh
uvicorn task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
```

## 规范架构

- 检索仅在服务端执行
- 后端索引方案为 `LanceDB-only`
- 规范数据源类型包括：
  - `tasks/active` 和 `tasks/archived` 下的任务卡片
  - 配置的 memory 目录下的 Markdown 记忆文件
  - 持久化在 `memory_objects.jsonl` 中的类型化客户端对象
  - 通过 `POST /ingest-structured` 摄入的结构化 JSON 负载

## 当前端点

### `GET /health`

匿名健康探测端点，用于运行时验证。

当前响应重点字段：

- `architecture: "lancedb-only"`
- `auth_configured`
- `embedding_configured`
- `lancedb_available`
- `scripts_present`
- `runtime_ready`
- `available_containers`
- `warnings`

### `POST /search`

在指定 container 的索引记忆中执行搜索。

请求：

```json
{
  "query": "string",
  "topk": 5,
  "container": "imac",
  "timeout_s": 120
}
```

响应结构：

```json
{
  "status": "ok",
  "command": ["python3", "/path/to/task_rag_search.py", "..."],
  "code": 0,
  "query": "string",
  "topk": 5,
  "container": "imac",
  "initialized": true,
  "message": null,
  "results": [
    {
      "score": 0.12,
      "taskId": "TASK-20260329-004",
      "docType": "client_ingest",
      "sourcePath": "tasks/rag/containers/imac/memory_objects.jsonl",
      "text": "..."
    }
  ],
  "stdout": "{...raw search payload...}",
  "stderr": ""
}
```

### `POST /embed`

将指定 container 的规范任务卡片、Markdown 记忆和类型化对象行重建到 LanceDB 中。

请求：

```json
{
  "container": "imac",
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

说明：

- 这是规范的重建入口端点
- 重建期间会保留已有的结构化摄入行

### `POST /ingest-memory`

执行规范的 LanceDB 重建，可选指定显式的 memory/archive 源目录。

请求：

```json
{
  "container": "imac",
  "memory_dir": null,
  "archive_dir": null,
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

### `GET /ingest-memory/contract`

显式暴露当前的摄入语义边界。

当前响应结构：

```json
{
  "mode": "lancedb-only",
  "content_source": "server-side-canonical-sources",
  "storage_location": "Canonical LanceDB rows live under WORKSPACE/tasks/rag/containers/<container>/lancedb.",
  "retrieval_scope": "Retrieval runs server-side against LanceDB only.",
  "notes": [
    "Use /ingest-memory/objects to persist typed objects into canonical server-side storage.",
    "Use /embed to rebuild task-card, markdown-memory, and typed-object rows into LanceDB.",
    "Use /ingest-structured for direct structured JSON-like ingest into LanceDB."
  ]
}
```

### `POST /ingest-memory/objects`

将类型化客户端对象持久化到指定 container 的规范服务端存储中。

请求：

```json
{
  "container": "imac",
  "objects": [
    {
      "id": "memory-001",
      "text": "Client-provided retrievable text.",
      "title": "Optional title",
      "source": "telegram",
      "tags": ["project_fact"],
      "metadata": {
        "project": "transcendence-memory"
      }
    }
  ]
}
```

响应结构：

```json
{
  "container": "imac",
  "accepted": 1,
  "stored_path": "/workspace/tasks/rag/containers/imac/memory_objects.jsonl",
  "stored_paths": ["/workspace/tasks/rag/containers/imac/memory_objects.jsonl"],
  "index_hint": "Run /embed for this container to refresh LanceDB after storing new objects."
}
```

### `POST /ingest-structured`

解析 JSON 格式的负载为语义块，并 upsert 到 LanceDB 中。

请求：

```json
{
  "container": "eva",
  "input_path": "/path/to/bookmarks.json",
  "doc_type": "structured_json",
  "doc_id": "chrome-bookmarks",
  "timeout_s": 120,
  "background": false,
  "wait": true
}
```

### `POST /build-manifest`

已弃用的空操作端点，仅为明确标记旧 manifest 阶段的退出而保留。

响应结构：

```json
{
  "command": [],
  "code": 0,
  "status": "deprecated",
  "note": "build-manifest was removed in LanceDB-only mode; use /embed."
}
```

### `GET /export-connection-token`

导出代理配对包。旧版 `token` 字段保持向后兼容，响应现在还包括：

- `pairing_auth`：用于手动设置的显式 `endpoint / api_key / container` 值
- `agent_onboarding.collect_from_user`：AI 安装器在导入前应询问用户的确切提示
- `agent_onboarding.tell_user`：AI 应主动告知的认证信息，而非静默配对

响应结构：

```json
{
  "token": "eyJlbmRwb2ludCI6Imh0dHBzOi8vcmFnLmV4YW1wbGUuY29tIiwiYXBpX2tleSI6InNrLXh4eCIsImNvbnRhaW5lciI6ImltYWMifQ==",
  "endpoint": "https://rag.example.com",
  "container": "imac",
  "note": "Base64 编码的连接令牌，附带引导提示和显式配对认证材料，用于 AI 辅助设置。",
  "pairing_auth": {
    "mode": "api_key",
    "endpoint": "https://rag.example.com",
    "api_key": "sk-xxx",
    "container": "imac",
    "accepted_headers": ["X-API-KEY", "Authorization: Bearer <api_key>"],
    "token_transport": "base64-json(endpoint, api_key, container)",
    "config_path": "~/.transcendence-memory/config.toml"
  },
  "agent_onboarding": {
    "collect_from_user": [
      {
        "id": "confirm_container",
        "title": "确认 container",
        "prompt": "我准备把你连接到 container \"imac\"。如果你想改成别的命名空间，请现在告诉我。",
        "reason": "让用户在导入前确认最终写入的 container。"
      }
    ],
    "tell_user": [
      "当前 skill 端鉴权模式为 api_key。",
      "connection token 内含 endpoint、api_key、container。"
    ],
    "recommended_commands": ["/tm connect <token-from-this-response>", "/tm connect --manual"]
  }
}
```

## 仓库证据

- `tests/test_task_rag_server_memory_objects.py` 覆盖了类型化对象持久化以及 LanceDB-only 路径上的 `/embed -> /search` 检索
- `scripts/smoke_test_client_ingest_search.py` 验证类型化客户端对象持久化到规范的 `memory_objects.jsonl`
- `docs/evolution/*` 记录了仓库从早期 manifest/FAISS 阶段到当前主线的演变过程
