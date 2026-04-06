# 环境变量参考 / Environment Reference

## 必需变量

| 变量 | 说明 | 示例 |
|------|------|------|
| `WORKSPACE` | 运行时根目录，包含 `tasks/`、`memory/` 等 | `$PWD`（server 仓库根目录） |
| `RAG_API_KEY` | API 认证密钥 | `sk-xxx` |
| `EMBEDDING_API_KEY` | Embedding provider 密钥 | `sk-xxx` |

## Embedding 相关变量

| 变量 | 说明 | 优先级 |
|------|------|--------|
| `EMBEDDING_BASE_URL` | Embedding provider endpoint | 最高（runtime 优先读取） |
| `EMBEDDINGS_BASE_URL` | Embedding provider endpoint（canonical 名称） | 次高 |
| `EMBEDDING_MODEL` | Embedding 模型名称 | `gemini-embedding-001` |
| `GOOGLE_EMBEDDING_BASE_URL` | Google embedding endpoint（备选） | — |

> **注意**：当前 runtime 解析顺序为 `EMBEDDING_BASE_URL` → `EMBEDDINGS_BASE_URL` → 默认值。
> 本地调试时建议两个变量同时设置为同一值，避免 shell 环境中残留旧值导致歧义。

## RAG 配置加载

如果使用 `load_rag_config.sh` 加载配置：

```bash
source ./scripts/load_rag_config.sh
```

该脚本从 `~/.config/transcendence-memory/rag-config.json`（或 `RAG_CONFIG_FILE` 覆盖路径）导出：
- `RAG_ENDPOINT`
- `RAG_AUTH_HEADER`
- `RAG_API_KEY`
- `RAG_DEFAULT_CONTAINER`

## 运行时目录结构

```
$WORKSPACE/
├── tasks/
│   ├── active/          # 活跃任务卡
│   ├── archived/        # 归档任务卡
│   └── rag/
│       └── containers/
│           └── <name>/  # 每个 container 的 LanceDB 数据
├── memory/              # Markdown memory 文件
└── memory_archive/      # 归档 memory
```

## 服务端口

| 配置项 | 默认值 |
|--------|--------|
| 监听端口 | `8711` |
| 监听地址 | `0.0.0.0` |

## 认证方式

支持两种 header：
- `X-API-KEY: <RAG_API_KEY>`
- `Authorization: Bearer <RAG_API_KEY>`

`/health` 端点为匿名访问，业务端点需要认证。
