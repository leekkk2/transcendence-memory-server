# Phase 04 — 技能接入与通用结构化入库

这是当前能回收到的最新阶段。

## 技能接入

后端不再只是独立服务，也被接入 OpenClaw 工作流：

- 安装并接入 memory enhancer skill
- 对齐 `tools/rag-config.json`
- 修复 `load_rag_config.sh`，正确导出：
  - `RAG_ENDPOINT`
  - `RAG_AUTH_HEADER`
  - `RAG_API_KEY`
  - `RAG_DEFAULT_CONTAINER`

## 从书签场景抽象出通用结构化入库

背景：

- Chrome Bookmarks 这类大型结构化 JSON，不适合继续走旧 fs / manifest 入口
- 需要先做结构化解析与语义切块，再写入 LanceDB

因此新增：

- `task_rag_structured_ingest.py`
- `POST /ingest-structured`

## 新正式链路

结构化数据现在可走：

- 结构化解析
- 语义切块
- embedding
- LanceDB 入库
- search

## 阶段意义

当前服务从“记忆文件增强检索”继续演化为：

- 面向多设备统一使用的 Eva 中心化 RAG 服务
- 面向技能工作流的增强层
- 面向任意 JSON / 树 / 列表 / 嵌套对象的通用结构化 RAG 入库后端
