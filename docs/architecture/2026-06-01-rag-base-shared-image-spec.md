# Spec: 通用 `rag-base` 共享多模态基础镜像 + base/diff 分层重构

> 状态：**需求已锁定，待实施**（2026-06-01 调研 + founder 决策；将在新对话升级实施）
> 关联：`memory-app-server/docs/architecture/2026-06-01-rag-base-full-and-async-ingestion-spec.md`（消费侧）、mvp-scaffold `docs/mvp-playbook/89-rag-anything-parser-as-a-service.md`（并行替代路线）

## 0. 需求（founder 已决策）

| 维度 | 决策 |
|---|---|
| 心智模型 | **base = 重的多模态共享层**（OS + 全 Python 依赖含 mineru/torch/opencv + mineru 模型缓存，**无业务代码**）；**diff = 各服务业务代码**（薄层）；**base + diff = full**。「lite vs full」与共享无关，按需保留 |
| Registry | **ghcr 公开**（`ghcr.io/leekkk2/rag-base`），OSS 友好，谁都能 pull；base 内仅开源依赖、无密钥 |
| 复用范围 | **通用 `rag-base`**（命名/版本独立于具体服务），供未来更多 RAG-Anything 系服务/MVP `FROM` 复用 |
| 消费方 | tm-server（本仓库，OSS）+ memory-app-server（私有派生）+ 未来服务 |
| 不在本次范围 | 共享代码包抽取（18 个共同 `.py` 的 DRY）——留作独立阶段 |

**目标**：两个服务都要 full（各自本地 mineru 解析），那 ~5GB 多模态重依赖**在 prod-host 上只存一份**（Docker 层去重），而非每个 full 各占一份；同时 OSS 仓库保持自包含。

## 1. 现状问题（当前 `Dockerfile`）

当前多阶段：`deps` → `deps-full`(=deps+`.[multimodal]`+mineru cache) → `runtime-base`(OS+user+**COPY scripts/ src/**) → `lite`(COPY deps site-packages) / `full`(COPY deps-full site-packages)。

两个达不到目标的结构性原因：
1. **`full` 把整个 site-packages 在一个 COPY 层揉进去**（L177-178），与 `lite` 的 site-packages 层是不同 blob → Docker **不去重**公共部分，`full ≠ base + diff`。
2. **`runtime-base` 把应用代码 `COPY scripts/ src/`（L124-125）烤进共享层** → 该层带 tm-server 代码，**memory-app 无法复用**为 base。

## 2. 目标分层（base 无代码 + 纯增量 diff）

```
ui-builder（不变，产出 admin SPA）
deps（不变：基础依赖 site-packages）
deps-full（不变：+ .[multimodal] + mineru 预热缓存）

# ── 通用基础（无业务代码，可发布复用） ─────────────
rag-sys-base   = python-slim + OS 库(libgl1/glib/gomp/poppler/libmagic/gosu) + 非 root 用户 tm + /data
                 ★ 不含 COPY scripts/src，不含 app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT
rag-base-lite  = FROM rag-sys-base + COPY --from=deps      site-packages         # 无多模态、无代码
rag-base       = FROM rag-base-lite + COPY --from=deps-full <多模态增量> + mineru cache   # ★ 那 ~5GB 共享重底座
                 （或 FROM rag-base-lite + RUN pip install .[multimodal] —— 二选一，见 §3）

# ── 各服务 = base + 业务 diff（本仓库内） ───────────
tm-lite        = FROM rag-base-lite + COPY scripts/ src/ + ui + app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT
tm-full        = FROM rag-base      + COPY scripts/ src/ + ui + app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT
```

要点：
- **app 级配置（ENV `WORKSPACE`/`PYTHONPATH`、`EXPOSE 8711`、`HEALTHCHECK`、`ENTRYPOINT`、`COPY scripts/src/ui`）从共享 base 下沉到 `tm-*` 服务阶段**——base 保持服务无关、通用可复用。
- `rag-base-lite` / `rag-base` 两个阶段**发布到 ghcr 公开**，供本仓库 + memory-app + 未来服务 `FROM`。

## 3. 关键实现决策：diff 层怎么生成（纯增量）

两种方式产出 `rag-base = rag-base-lite + 多模态 diff`：

- **方式 A（推荐）`FROM rag-base-lite; RUN pip install --constraint constraints.txt ".[multimodal]"`**
  - 多模态包**叠加**在 base-lite 已装的 site-packages 之上 → 新层 = 增量多模态包（torch/mineru/opencv）。这是真正的 `base + diff`，Docker 共享 base-lite 层。
  - 前提（**待验证假设，非 constraints 强保证**）：理想情况下多模态 extra **只新增**、不回改 base-lite 已有包（否则 diff 层夹带 churn）。但 `constraints.txt` 当前**仅 exact-pin 2 个 opencv-headless（`==4.10.0.84`）**，`pyarrow` 只有下界 `>=15`、`numpy` **故意不 pin**（注释：1.x 无 py3.13 wheel）——锁面窄，无法机械保证「纯增量」。仅当多模态包恰好不要求改 numpy/pyarrow 时该假设才成立，**必须靠 §7 验收的 `docker history` 体积断言兜底（该断言已升为 Phase 1 必做 CI 门禁）**。
- 方式 B：`FROM rag-base-lite; COPY --from=deps-full <仅 delta>` —— 难精确隔离 delta，不推荐。

→ **采用方式 A**。`pyproject.toml` 的 `[project.optional-dependencies] multimodal = [mineru, ...]` 已具备；`constraints.txt` 已存在（含 opencv-headless pin）。

## 4. 命名与版本

- 镜像名：`ghcr.io/leekkk2/rag-base`（generic，**不绑定 tm-server**）+ 可选 `ghcr.io/leekkk2/rag-base-lite`。
- Tag：与具体服务版本**解耦**。建议 `rag-base:<rag-anything主版本>-py<pyver>`（如 `rag-base:1.3-py3.13`）或日历版 `rag-base:2026.06`。
- 消费侧**显式 pin**（如 `FROM ghcr.io/leekkk2/rag-base:1.3-py3.13`），升级 base 时各服务重建——这就是 Podfile 式「声明版本依赖」。

## 5. CI / 发布（GitHub Actions）

本仓库 R1 约束：远端生产**只 `docker pull` 不 build**；镜像由 CI/buildx 产出。
1. 新增/改 workflow：`docker buildx build --target rag-base-lite` + `--target rag-base`，多 arch，`push` 到 `ghcr.io/leekkk2/rag-base{,-lite}:<ver>`（GITHUB_TOKEN 有 packages:write）。
2. 构建 `tm-lite`/`tm-full`（`FROM` 本地 base 阶段或已发布 tag）→ push `transcendence-memory-server:<ver>-{lite,full}`。
3. base 与服务镜像版本在 release notes 关联记录。

## 6. OSS 自包含性（关键约束）

- **Dockerfile 内仍含全部阶段**（`rag-sys-base`→`rag-base-lite`→`rag-base`→`tm-*`）→ `docker build --target tm-full` 可**完全自建**，不依赖任何外部已发布镜像。
- 已发布的 `ghcr.io/leekkk2/rag-base` 是**复用/省盘优化**，不是构建前提。OSS 使用者二选一：自建（自包含）或 pull base（省时）。
- 结论：**开源完整性零损失**，base 镜像是叠加的便利层。

## 7. 验收

1. `docker build --target rag-base -t rag-base:test .` 成功；`docker history` 显示多模态在**独立增量层**之上 base-lite 层。**（Phase 1 必做 CI 门禁，不再是建议项）** 该体积断言是「去重收益不被 numpy/pyarrow churn 打折」的唯一保障——因 `constraints.txt` 锁面窄（仅 opencv exact-pin），纯增量不是 constraints 强保证而是待验证假设（见 §3 方式 A）。
2. `FROM rag-base:test` 构建 `tm-full` 仅新增薄代码层（`docker history` 末尾几层为 scripts/src/ui，MB 级）。
3. 镜像体积：`rag-base` ≈ 5–6GB；`tm-full` = `rag-base` + 薄层；`rag-base-lite`/`tm-lite` ≈ 0.8–1GB。
4. `tm-full` 运行 `/health` build_flavor=full + 一次 `/documents/file` PDF 摄取走 mineru 正常。
5. 多消费者去重验证：拉 `rag-base` 后构建/拉取 `memory-app:full`，`docker system df` 显示 base 层共享、未翻倍。

## 8. 风险 & 回滚

- base/diff 版本偏移：消费侧 pin 精确 tag；base 升级走 release + 通知两服务重建。
- 多模态 diff 夹带 churn：靠 `constraints.txt` 锁版本；CI 加 `docker history` 体积断言。
- 回滚：保留旧 `transcendence-memory-server:<ver>-full`（自包含整镜像）；切回旧 Dockerfile 即恢复原构建。
- prod-host 实施按 `--pull never` 铁律 + 先清磁盘（见消费侧 spec §C）。

## 9. 与并行路线（mvp-scaffold §89 parser-as-a-service）的关系

§89 是另一条省盘路线：把 mineru 抽成独立 `mineru-api` 服务、两 app 跑 lite。**本 spec 选的是相反取向**：两 app 都 full、但**共享同一个重 base**。两者省盘量相当；本方案**每个服务自给自足、无运行时解析服务依赖**，运维更简单（founder 已选此路）。未来若要独立扩缩解析，可再叠加 §89。
