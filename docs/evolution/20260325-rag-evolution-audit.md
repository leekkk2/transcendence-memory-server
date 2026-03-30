# Transcendence Memory Server 演变审计（截至 2026-03-25）

> 目的：把 **git 仓库未完整记录**、但已实际发生在 Eva / OpenClaw 工作区 / 相关会话中的服务端演进过程补整理出来，方便回看它是怎么一步步收敛到当前 `transcendence-memory-server` 形态的。

## 0. 审计范围

本次整理来源包括：

1. 当前仓库：`~/.openclaw/rag-everything`
2. Eva 工作区现状：`~/.openclaw/workspace/scripts/*task_rag*`
3. 工作区记忆：`memory/*.md`
4. 交接/运维文档：`docs/ops/*rag*`
5. 技能资料：`skills/rag-everything-enhancer/*`
6. 工作区 git 提交记录中与 RAG 相关的历史线索

> 说明：这不是“原汁原味重建所有真实提交历史”，而是把目前还能回收的事实、文件与阶段性结论，尽可能补全为一条可读的演进链路。

---

## 1. 仓库原始状态（git 中仅存的基线）

当前 `rag-everything.git` 仓库在本次整理前只有一个初始提交：

- `14a9043` `init rag-everything`

该基线版本的特征：

- 主要脚本仅有：
  - `scripts/task_rag_server.py`
  - `scripts/task_rag_search.py`
  - `scripts/task_rag_embed.py`
  - `scripts/task_rag_build_manifest.py`
  - `scripts/task_rag_ingest_memory_refs.py`
- 架构仍是早期模式：
  - `manifest.jsonl`
  - `FAISS`
  - `SQLite`
- 服务入口已指向 Eva：
  - `https://rag.zweiteng.tk`
  - `127.0.0.1:8711`
- README 中已经明确：
  - 统一部署在 Eva
  - iMac / Eva / Aliyun 共用
  - Aliyun 不本地安装

也就是说：**仓库里保留了“最早能跑通的 Eva 中心化版本”，但没有后续几轮大改。**

---

## 2. 第一阶段：从任务记忆 / RAG-Anything 想法，落到 Eva 中心化 RAG 脚手架

从工作区记忆 `memory-imac/2026-03-18.md` 与相关 RAG 容器数据可回收出以下阶段事实：

### 已发生的关键决策

- 用户批准将 RAG 方案落到 Eva 上统一部署。
- 明确要求：
  - **RAG-everything 统一部署在 Eva**
  - **供 iMac / Eva / Aliyun 共用**
  - **Aliyun 不本地安装**

### 当时已落地的内容

- 创建任务卡：`TASK-20260318-001-rag-anything-eva-openclaw`
- 初版脚手架已成型：
  - `task_rag_build_manifest.py`
  - `task_rag_ingest_memory_refs.py`
  - `task_rag_embed.py`
  - `task_rag_search.py`
- 设计口径：
  - manifest 构建
  - embedding
  - search
  - 双存储（FAISS + SQLite）

### 阶段特征

这一阶段本质上是：

- 从“任务记忆增强 / RAG-Anything”思路
- 过渡到“可部署的 Eva 中心化 task RAG 服务”
- 还没有进入 LanceDB-only 架构

---

## 3. 第二阶段：服务化上线到 Eva（8711 / Nginx / 域名 / systemd）

后续从工作区文档、技能资料和系统侧痕迹可以确认，服务化部署已完成：

### 服务与网关层

- 正式域名：`https://rag.zweiteng.tk`
- 内部端口：`127.0.0.1:8711`
- systemd 服务：`rag-everything.service`
- Nginx 反代：`rag.zweiteng.tk -> 127.0.0.1:8711`
- TLS：Let’s Encrypt / Certbot

### 可回收证据

- `/etc/systemd/system/rag-everything.service`
- `/etc/nginx/sites-available/rag.zweiteng.tk`
- `/etc/letsencrypt/renewal/rag.zweiteng.tk.conf`
- 工作区技能引用文档：
  - `references/OPERATIONS.md`
  - `references/setup.md`
  - `references/EVA_CENTRAL_MODE.md`

### 阶段意义

这一步说明 RAG-everything 不再只是本地脚本集合，而是已经成为 **Eva 上正式对外提供的记忆检索服务**。

---

## 4. 第三阶段：从 FAISS/SQLite/manifest 迁移到 LanceDB-only

这是目前能回收到的**最大一次架构演进**。

### 时间线证据

来自 `memory/2026-03-22-task-coordination.md`、`memory/2026-03-23.md`、`memory/2026-03-23-rag-cleanup.md`、`docs/ops/rag-eva-backend-handover-20260323.md`：

### 核心变化

旧方案：

- `manifest.jsonl`
- `FAISS`
- `SQLite`
- `task_rag_embed.py` 基于 manifest 做嵌入

新方案：

- **LanceDB-only 主路径**
- 新增：`scripts/task_rag_lancedb_ingest.py`
- 改造：
  - `task_rag_server.py`
  - `task_rag_search.py`
- `/embed` 改为 LanceDB ingest
- `/search` 改为 LanceDB 查询
- `/ingest-memory` 改为直接走 LanceDB memory ingest
- 容器数据路径转为：
  - `tasks/rag/containers/<container>/lancedb`

### 迁移目标

- 尽量保持前端 / 技能层调用无感
- 接口路径继续保留：
  - `/search`
  - `/embed`
- 只是后端底层从 JSON/FAISS/SQLite 切换为 LanceDB

### 可回收的关键结论

- `eva` 容器完成过 LanceDB 重建
- `imac` 容器完成过 LanceDB 重建
- 文档中多次强调：
  - **前端理论上可无感使用**
  - 但是否正式验收通过，还取决于服务稳定性与真实链路验证

### 与仓库当前状态的差异

当前仓库原始内容仍停留在 FAISS/SQLite 版；
而工作区的真实运行版本已经进入 **LanceDB-only**。

这正是本次补分支最重要的原因。

---

## 5. 第四阶段：服务故障、修复、健康检查补齐

从 `memory/2026-03-22-docker-sysctl.md`、`docs/ops/rag-eva-embed-repair.md`、`memory/2026-03-23.md` 可回收出一段很关键的“线上事故演化”。

### 事故经过

- 在修改 `task_rag_server.py` 以支持更稳的长任务 / 后台运行时
- 一度把服务脚本改坏
- 报错为：
  - `SyntaxError: invalid syntax`
- 导致：
  - `rag-everything` 服务起不来
  - `rag.zweiteng.tk` 从公网侧出现 `502`

### 修复动作

- 恢复 `task_rag_server.py` 正确语法
- 重启 `rag-everything.service`
- 恢复本机 `/search` 可用
- 恢复公网入口

### 后续增强

- 为 `task_rag_server.py` 增加 `GET /health`
- 将健康检查路由改为**匿名可读**
- 保持业务接口继续要求鉴权
- 在验收中将 `/health` 纳入标准检查项

### 阶段意义

这说明系统已经从“能跑”进入“真实运维”的阶段：

- 要考虑线上恢复
- 要考虑 health endpoint
- 要考虑 systemd / Nginx / 业务脚本三层联动

---

## 6. 第五阶段：新容器初始化行为与 LanceDB 兼容性修正

从 `docs/ops/rag-new-container-behavior-20260323.md`、`memory/2026-03-23.md`、技能 references 中可以看到后续又补了一轮“可分发性修正”。

### 解决的问题

#### 6.1 新容器首次搜索报错

旧行为：
- 新容器若还没完成 embed / LanceDB 初始化
- `/search` 直接把底层错误暴露出去，例如：
  - `Table 'chunks' was not found`

新行为：
- 改成结构化返回
- 明确告诉调用方：
  - 当前容器尚未初始化
  - 需要先 `/embed`

#### 6.2 LanceDB `list_tables()` 返回形态差异

不同环境 / 版本下，`db.list_tables()` 返回值结构不一致，曾引发：
- `unhashable type: 'list'`

后续 `task_rag_search.py` 已补兼容处理。

### 阶段意义

这一阶段不是“新功能”，而是把后端从“能在当前机器上跑”推进到“适合未来多容器、多设备分发”的状态。

---

## 7. 第六阶段：技能化与工作流接入

从 `memory/2026-03-24.md`、`docs/ops/20260324-EVA-TO-ALL-RESULT-RAG-SKILL-INTEGRATED-AND-WORKSPACE-INGESTED.md` 可确认：

### 已完成事项

- 安装并接入 `skills/rag-everything-enhancer`
- 对齐工作区 RAG 配置：
  - `tools/rag-config.json` -> `https://rag.zweiteng.tk`
  - 默认容器 -> `eva`
- 修复 `scripts/load_rag_config.sh`
  - 之前因错误使用 `open("")` 无法正确读取配置
  - 后续修复为稳定导出：
    - `RAG_ENDPOINT`
    - `RAG_AUTH_HEADER`
    - `RAG_API_KEY`
    - `RAG_DEFAULT_CONTAINER`
- 通过正式入口验证 `search`
- 再次触发后台 embed

### 阶段意义

这一步说明 RAG-everything 已不只是后端服务，也不只是运维组件，而是已经被包装成 **OpenClaw 可复用技能 / 工作流增强层**。

---

## 8. 第七阶段：从书签场景抽象出通用结构化入库

这是目前能回收到的**最新一次功能演进**。

### 证据来源

- `memory/2026-03-24.md`
- `docs/ops/20260324-EVA-TO-ALL-RESULT-RAG-GENERIC-STRUCTURED-INGEST-IMPLEMENTED.md`

### 背景

原先在处理 Chrome Bookmarks 等大型结构化 JSON 时，通用 fs / manifest 链路并不合适：

- 可能只有文件 meta
- 没有真实语义 chunk
- 导致“可入库但不可检索”或“0 有效结果”

### 后续抽象出的正式能力

新增：
- `scripts/task_rag_structured_ingest.py`

服务接口新增：
- `POST /ingest-structured`

### 新能力的本质

将数据处理流程从：

- “把文件当普通文本/普通 fs 对象处理”

升级成：

- **结构化解析 → 语义切块 → embedding → LanceDB 入库 → search**

### 阶段意义

这表明 RAG-everything 已经从“记忆文件检索增强”进一步成长为：

- **可接纳任意 JSON / 树 / 列表 / 嵌套对象的通用结构化 RAG 入库后端**

---

## 9. 当前真实版本与原仓库版本的主要差异

本次整理前：

### 原仓库（仅 `init rag-everything`）

- FAISS + SQLite + manifest 路线
- 无 `/health`
- 无后台 embed 控制
- 无 LanceDB ingest
- 无结构化 ingest
- README 较简

### 当前 Eva 工作区真实运行版

- LanceDB-only 主路径
- `/health` 匿名开放
- `/embed` 支持 background / wait 模式
- `/search` 具备容器初始化态返回
- 兼容 LanceDB `list_tables()` 返回差异
- 新增 `task_rag_lancedb_ingest.py`
- 新增 `task_rag_structured_ingest.py`
- 新增 `/ingest-structured`
- `load_rag_config.sh` 已修复
- 技能文档、运维文档、交接文档已比较完整

---

## 10. 本次新分支补录了什么

本分支不是“伪造旧提交历史”，而是做以下两件事：

### 10.1 把当前真实后端实现补进仓库

已从 Eva 工作区补入：

- `scripts/task_rag_server.py`
- `scripts/task_rag_search.py`
- `scripts/task_rag_lancedb_ingest.py`
- `scripts/task_rag_structured_ingest.py`
- `scripts/load_rag_config.sh`
- `scripts/run_task_rag_server.sh`

### 10.2 把能回收到的演变链整理成文档

新增本审计文档：

- `docs/evolution/20260325-rag-evolution-audit.md`

---

## 11. 仍然缺失 / 无法百分百重建的部分

以下内容目前仍无法完全重建为原始历史：

1. **中间每一次真实提交 hash**
   - 工作区记忆提到过：`49df27c`、`f31208b`
   - 但它们不在当前 `rag-everything.git` 仓库中
   - 更可能存在于工作区 repo 或其他临时仓库 / 未推送状态

2. **最初从仓库版到工作区运行版的逐提交 diff 链**
   - 现在只能根据：
     - 工作区现有文件
     - 记忆文档
     - 交接文档
     - 会话落档
   - 重建阶段性结论

3. **某些修复是否先发生在其他机器 / 会话后再同步到 Eva**
   - 例如 iMac / Aliyun 参与的那部分，仅能恢复“结果与结论”，未必能精确还原全部操作顺序

---

## 12. 结论

如果只看 `rag-everything.git`，你会误以为它还停留在：

- 初版 Eva 中心化
- FAISS/SQLite/manifest
- 没有后续大规模演化

但从工作区、文档、记忆、技能资料综合起来看，真实演进路径已经清楚很多：

1. **RAG-Anything / 任务记忆增强构想落地为 Eva 中心化 RAG**
2. **完成 8711 + Nginx + 域名 + systemd 服务化部署**
3. **从 FAISS/SQLite/manifest 迁移到 LanceDB-only**
4. **经历线上事故与恢复，补齐 `/health` 和服务稳定性修复**
5. **修正新容器初始化与 LanceDB 兼容性问题**
6. **接入 `rag-everything-enhancer` 技能，进入正式工作流**
7. **继续演化出通用结构化入库能力**

换句话说：

> 这个项目已经不是“最初那个小型 task RAG repo”了，而是演变成了 **Eva 上统一提供给多设备、多工作流使用的 RAG 服务与增强层**。

---

## 13. 建议的下一步

如果你想继续把这段历史做得更完整，我建议后续再做两件事：

1. **把工作区 repo 中与 RAG 相关的提交（如 `49df27c` / `f31208b` / `dfdade3`）逐个摘出来，对照整理成时间线附录**
2. **补一个 `CHANGELOG-EVOLUTION.md`，用“阶段 + 影响面 + 兼容性 + 未完成项”的格式持续维护**

这样以后看这个项目，就不会再只看到一个 `init rag-everything` 了。
