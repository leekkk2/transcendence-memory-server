# Phase 01 — 初始 Eva 中心化服务基线

这一阶段对应当前服务最早可见基线（仓库初始提交 `init rag-everything`）及其明确目标：

- 服务统一部署在 Eva
- iMac / Eva / Aliyun 共用
- Aliyun 不本地安装
- 对外服务入口：`https://rag.zweiteng.tk`
- 内部反代：`127.0.0.1:8711`

## 当时的核心实现

- `task_rag_build_manifest.py`
- `task_rag_ingest_memory_refs.py`
- `task_rag_embed.py`
- `task_rag_search.py`
- `task_rag_server.py`

## 架构特征

- `manifest.jsonl` 构建
- `FAISS` 索引
- `SQLite` 元数据 / 向量回退
- FastAPI 服务化入口

## 备注

这个阶段是当前服务的历史可运行起点，但还不是后来的 LanceDB-only 正式形态。
