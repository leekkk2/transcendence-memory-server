# Phase 1 设计 · RAG 优雅降级根治 + 信源溯源/分数硬拦截增量(蓝图 §1/§3/§5)

> 日期 2026-06-09 · 基线 main@d53051e(应用代码 = prod v0.18.0)· 蓝图 `docs/面向Agent工程的RAG系统与自动化治理架构蓝图.md` §1/§3/§5

## 1. 问题(已用生产数据确诊)

admin UI 搜索 `<container>` 返回 `搜索失败：<container>_openai: not_initialized; <container>: timeout`,整条搜索失败。根因:

- prod `profiles.yaml` `union_search_default: true` → 搜主容器自动连带查双轨镜像 `<container>_openai`,但该镜像目录只有空 `__manifest`、无 `chunks.lance`(从未 embed)→ `task_rag_server.py:1170` 返 `container_not_initialized` → `:1441` 标 `not_initialized`。
- 主容器 `<container>`(63M)在 union 多容器分支被压进 `_DEFAULT_PER_CONTAINER_TIMEOUT_S=12.0`(`:1348/:1386-1388`)→ 冷启动+网关 embedding 超 12s → `timeout`(`:1434/:1462`)。
- 优雅降级骨架**已存在**(`has_any_ok:1506`、`degraded:1515`、`per_container_status`、`union_applied`),但主容器被 12s 误杀致 `has_any_ok=False` → `status='error'`(`:1526`)+ 报错文案(`:1519-1521`)。

**Task 2 根治 = 蓝图 §3 第一增量**:有任一容器(尤其主容器)出结果就降级返回,而非整体失败。

## 2. 研究纠偏(websearch+context7,实现必须遵守)

- **度量 = LanceDB 默认 L2 + 向量未归一化**(证据 `task_rag_lancedb_ingest.py:679/981/1099` 无 metric/index;`task_rag_runtime.py:225` 无归一化;`task_rag_server.py:1197-1200` `score=_distance`;`models:124-127` "Smaller is better")。**`hit.score` 是 L2 距离,越小越相关,无 `1-score` 相似度换算。蓝图「0.7 相似度→score>(1-0.7)」公式在本仓直接套用会错。**
  - → **score-gate 走路线 A(距离阈值):丢弃 `score > threshold_distance` 的 chunk**;`score is None` 一律拦。**默认关闭(opt-in)**,阈值是原始 L2 距离上界(非 0.7 相似度),需实测标定。不强行换算相似度。
- **HTTP 语义 = 200 + body 标志**(206 是 Range 误用、207 是 WebDAV bulk-write,均不适)。部分成功(有结果)→ 200 + `is_degraded`;**全失败维持现状 200 + body `status='error'`**(转 503 是更诚实的设计,但触及前端 ApiError → 记 Phase 2,本期不碰)。降级响应建议 `Cache-Control: no-store`(可选)。
- **/query score-gate 可行**:`aquery` 前用 `_run_single_search(query, 1, container, ...)` 取 top1 距离预检,超阈值不调 LLM,~10 行,**默认关闭**。无容器目录映射坑(同一 container 根)。`/query` 的 LLM citation injection(`aquery_data()` 取 chunks 的 file_path/source_id)工程量大 → **Phase 2**。
- **skill 维度已正确**:references 已普遍是 `text-embedding-3-small/1024` → Task 8 改为「核对 + 定向补端点/troubleshooting + 版本 bump」,**不动 `tm/SKILL.md`**,description 触发语义未变无需优化环。

## 3. 改动(外科手术,复用骨架,不重写邻近代码)

### 项3 · per-container timeout 自适应(止血核心)
- `profiles_loader.py`:`ProfileSet` 增 `union_per_container_timeout_s: float = 30.0`(仿 `union_search_default` dataclass+解析)。
- `task_rag_server.py`:加 helper `_get_union_per_container_timeout()`(仿 `_get_union_search_default:1037-1050`,异常回退 `_DEFAULT_PER_CONTAINER_TIMEOUT_S`);`:1386` 改 `per_container_timeout = req.per_container_timeout_s or _get_union_per_container_timeout()`。
- 仅触 `:1387 len(targets)>1` union 分支;`:1388 min(.., req.timeout_s)` 封顶不动;单容器 600s 旧路径零改。
- ⚠️ `SearchReq.per_container_timeout_s` 现 `le=30.0`(`models:69`):默认 30 正好贴上限,不必放宽 `le`;若实现期发现 30s 仍紧再议。

### 项2 · not_initialized sibling 软跳过
- `task_rag_server.py:_resolve_search_targets`:`:1124 targets.append(sibling)` 前插入「`chunks` 表存在」探测,未就绪则 `return targets, False`(沿用 `:1118-1122` 既有前置过滤风格)。
- 新增 helper `_container_has_chunks_table(name) -> bool`:`lancedb.connect(lancedb_dir(name))` + `'chunks' in db.table_names()`,**只连不 embedding**,任何异常吞为 `False`。
- 保留聚合处 `:1440-1442` not_initialized 分支作防御(sibling 解析后被清空仍不拖垮)。sibling 日后 embed 就绪 → 下次自动恢复双轨。

### 项1 · 降级元数据补全(复用 degraded/has_any_ok)
- `models.py:SearchResponse`(`:146-181`)增带默认值字段:`is_degraded: bool = False`、`fallback_source: str | None = None`。
- `task_rag_server.py:1525-1543` 构造时派生:`is_degraded = degraded`(同值双写,Agent 读新名/前端读旧名);`fallback_source = 'partial_containers' if (has_any_ok and degraded) else None`。
- `:1519-1523` message:`not has_any_ok` 分支不变(全失败才拼人话);新增 `elif has_any_ok and degraded and not rerank_warning: message = None`(部分成功不弹红条)。
- `union_applied/degraded/has_any_ok` 赋值点(`:1506/:1515/:1540`)**不动**。保持 HTTP 200(代码注释说明刻意 200 + Phase2 可选 503)。

### 项4 · citation 数组 + 分数硬拦截(默认关闭)
- `profiles_loader.py`:`ProfileSet` 增 `similarity_threshold: float | None = None`(**None=关闭** score-gate;语义=L2 距离上界,非相似度)+ `citation_enabled: bool = True`。
- `models.py`:新增 `Citation`(`{chunkId, sourcePath, section?, score, container?}`);`SearchResponse` 增 `citations: list[Citation] | None = None` + `blocked_low_score: int = 0`;`QueryResponse` 增 `top_score: float | None`(score-gate 命中时 `status='score_gated'`)。`SearchReq`/`QueryReq` 各增 `score_threshold: float | None = None`(None=用 profiles 默认/关闭,≤0=显式关闭)。
- `/search`:`:1504 merged` 之后,若 `eff_threshold`(请求级>profiles)生效:`blocked = [h for h in merged if h.score is None or h.score > eff_threshold]`;`merged = [h for h in merged if h not in blocked]`;`blocked_low_score = len(blocked)`;无结果时 body 加结构化标记(不报错)。citation_enabled 时由 `merged` 投影 `citations`。
- `/query`(`query_rag:3127-3161`):`aquery` 前 `_run_single_search(req.query, 1, canonical, timeout)` 取 top1 `score`;若 `eff_threshold` 生效且 `top1.score > eff_threshold`(或未初始化)→ 返 `QueryResponse(status='score_gated', answer='', top_score=...)`,不调 LLM。`eff_threshold` 为 None 时**行为与现状逐字节一致**。

### 项5 · 容器初始化状态透出(后端零改 + 前端)
- 后端 `/index-status`/`/containers/{name}/index-status` 已够。前端见下。

### 前端 · `dashboard/src/pages/Memory.tsx` + locales
- `SearchResponse` 类型扩 `degraded?/is_degraded?/per_container_status?/containers?/fallback_source?/blocked_low_score?`。
- 部分成功不当 error(仅 `status==='error'` 才红);新增 info 色降级提示条列非 ok 容器及状态;`not_initialized` → "该镜像未初始化,请先 embed";降级时仍渲染 `resp.results`。新增 i18n key `memory.degradedHint`/`memory.notInitializedHint`(`locales/en.json`+`zh-CN.json`)。

## 4. 测试(扩 `tests/test_search_union_routing.py`,复用 `_fake_embedding_server`+`_load_e2e_server`)
- **复现(红)**:主容器 ok + 未初始化 sibling(无 chunks 表)+ union → 修复前 `status='error'`;修复后 sibling 软跳过 → `containers==[main]`、`union_applied==False`、无 `not_initialized` 噪音。
- **降级**:主 ok + 第三容器失败 → `status='ok'`、`is_degraded==True`、`fallback_source=='partial_containers'`、`results` 非空;**全失败 → `status='error'`**。断言 `is_degraded==degraded`。
- **阈值**:`score_threshold` 极小(距离上界)→ `blocked_low_score>0`、`results` 被拦;`score_threshold=None` → 行为与现状一致。`/query` score_threshold 生效 → `status='score_gated'` 不调 LLM。
- 回归全绿:`tests/test_search_union_routing.py`(全 13+新)、`test_search_rerank_integration.py`、`test_cli/test_search.py`。
- **测试配方**(本机 OrbStack):`docker run --rm -u root --entrypoint python -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/tests:/app/tests:ro" -w /app tm-server:dev-test -m pytest tests/<file> -v`

## 5. 不在本期(Phase 2+)
- `/query` LLM citation injection(`aquery_data()` 抽 file_path/source_id,需先验 ingest 喂了 file_path)。
- 全失败转 HTTP 503(`Response` 注入,触及前端 ApiError)。
- 显式切 `.distance_type("cosine")` 使蓝图 0.7 相似度公式成立(需配套 index 同步)。
- score-gate 默认阈值的经验标定(实测 `_distance` 分布取分位点)。
