# Phase 02 — 从 FAISS/SQLite/manifest 迁移到 LanceDB-only

这是目前能回收到的最大一次后端架构变更。

## 迁移前

旧路径依赖：

- `manifest.jsonl`
- `FAISS`
- `SQLite`
- `task_rag_embed.py` 做 manifest → embedding → 索引构建

## 迁移后

新主路径：

- `LanceDB-only`
- `task_rag_lancedb_ingest.py` 成为核心入库入口
- `/embed` 改为 LanceDB ingest
- `/search` 改为 LanceDB 查询
- `/ingest-memory` 直接走 LanceDB memory ingest
- 容器数据路径切到：`tasks/rag/containers/<container>/lancedb`

## 兼容目标

- 前端 / 技能层尽量无感
- API 路径仍保持：
  - `/search`
  - `/embed`

## 阶段证据来源

工作区中已回收到的事实包括：

- `eva` 容器完成过 LanceDB 重建
- `imac` 容器完成过 LanceDB 重建
- 工作区记忆中提到历史提交：
  - `49df27c`：重构 RAG 后端为 LanceDB-only 并重建 eva 数据
  - `f31208b`：清理旧版 RAG manifest 与嵌入产物

## 备注

这些提交不在当前 `rag-everything.git` 仓库可见历史里，因此本阶段属于“补录演进事实”，不是重放原始 commit 对象。
