# Phase 03 — 服务稳定性修复、健康检查与初始化兼容

这一阶段主要不是新功能，而是把当前服务从“能跑”推进到“更适合长期运行和分发”。

## 线上事故与恢复

曾发生：

- 修改 `task_rag_server.py` 支持更稳的长任务 / 后台运行时
- 一度写坏服务脚本
- 导致 `SyntaxError`，服务无法启动
- `rag.zweiteng.tk` 外部表现为 `502`

后续完成：

- 修复 `task_rag_server.py`
- 恢复 `rag-everything.service`
- 恢复 Nginx → 8711 → FastAPI 正常链路

## 健康检查

新增：

- `GET /health`

策略：

- `/health` 匿名开放
- `/search`、`/embed`、`/ingest-memory` 等业务接口继续要求鉴权

## 新容器初始化体验

修复问题：

- 容器尚未初始化 LanceDB 时，`/search` 不再直接暴露底层错误
- 改为返回结构化状态，明确提示先执行 `/embed`

## LanceDB 兼容性

兼容不同 LanceDB 版本下 `list_tables()` 返回形态差异，避免：

- `unhashable type: 'list'`

## 阶段意义

这一阶段标志着系统进入真实运维状态：

- 有服务恢复要求
- 有匿名 health endpoint
- 有更清晰的初始化态返回
- 有跨版本兼容修补
