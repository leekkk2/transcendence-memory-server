# 实施附录：`rag-base` 目标 Dockerfile 全文 + CI 发布

> 状态：**实施附录**（2026-06-01，配套主 spec `2026-06-01-rag-base-shared-image-spec.md`）
> 角色：本文件给出可**整文件替换**的目标 `Dockerfile`、可粘贴的 GitHub Actions 改动、OSS 自包含验证步骤、风险与体积断言。
> 前置阅读：主 spec §2（目标分层）/§3（方式 A 纯增量 diff）/§6（OSS 自包含）。
> 当前真实代码基线：`Dockerfile`（188 行，阶段 `ui-builder`/`deps`/`deps-full`/`runtime-base`/`lite`/`full`），`pyproject.toml` v0.17.3，`constraints.txt`（opencv headless pin），`.github/workflows/ci.yml`（test → docker-validate → publish-docker[Docker Hub, push-by-digest] → merge-manifests）。

---

## 0. 重构前后阶段对照（先看清"改了什么"）

| 旧阶段 | 新阶段 | 变化 |
|---|---|---|
| `ui-builder` | `ui-builder` | **不变** |
| `deps` | `deps` | **不变** |
| `deps-full` | `deps-full` | **不变** |
| `runtime-base`（OS 库 + user + /data + **COPY scripts/src** + app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT） | `rag-sys-base`（OS 库 + user + /data，**仅此**） | **拆**：剥离全部业务代码 + app 级配置，下沉到 `tm-*`。这是让 base "无代码、可复用" 的核心改动。 |
| —（无） | `rag-base-lite`（`FROM rag-sys-base` + COPY `deps` site-packages + uvicorn bin） | **新增**：无多模态、无代码的发布层。 |
| —（无） | `rag-base`（`FROM rag-base-lite` + `RUN pip install ".[multimodal]"` + COPY mineru cache） | **新增**：~5GB 共享重底座，纯增量 diff（方式 A）。 |
| `lite`（`FROM runtime-base` + COPY `deps` site-packages + 代码 + ui） | `tm-lite`（`FROM rag-base-lite` + COPY scripts/src/ui + app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT） | **改 FROM + 下沉 app 配置**：site-packages 不再在此 COPY（已在 base-lite），本阶段只贴薄代码层 + app 配置。 |
| `full`（`FROM runtime-base` + COPY `deps-full` 整 site-packages + mineru cache + 代码 + ui） | `tm-full`（`FROM rag-base` + COPY scripts/src/ui + app ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT） | **改 FROM + 去掉整 site-packages COPY**：多模态包与 mineru cache 已在 `rag-base`，本阶段只贴薄代码层 + app 配置 → `tm-full = rag-base + 薄 diff`。 |

> 旧 `lite`/`full` 名称在本仓库 CI / compose / deploy 脚本里被引用为 `--target lite` / `--target full`。新 Dockerfile 把最终服务阶段**重命名**为 `tm-lite` / `tm-full`，CI 已同步改（见 §2）。compose / deploy 拉的是已发布 tag（`:<ver>-{lite,full}`），不依赖阶段名，**无需改**。

---

## 1. 完整目标 Dockerfile（可整文件替换）

> 直接覆盖仓库根 `Dockerfile`。每个阶段顶部注释标注"改了什么 / 为何"。

```dockerfile
# syntax=docker/dockerfile:1.7
# =============================================================================
# transcendence-memory-server — multi-stage container build
#
# v2 分层（2026-06-01 rag-base 重构）：base 无业务代码、可发布复用；服务 = base + 薄 diff。
#
# Stages:
#   ui-builder     produces /app/static/admin (Vite-built React SPA)        [不变]
#   deps           runtime Python deps from pyproject.toml + constraints     [不变]
#   deps-full      adds [multimodal] extras + pre-warms mineru models        [不变]
#   ── 通用基础（无业务代码，发布到 ghcr.io/leekkk2/rag-base{,-lite}） ──
#   rag-sys-base   OS libs + non-root user + /data  ★无代码/无 app 配置       [由旧 runtime-base 拆出]
#   rag-base-lite  rag-sys-base + deps site-packages（无多模态、无代码）       [新增·可发布]
#   rag-base       rag-base-lite + .[multimodal] 纯增量 + mineru cache        [新增·可发布·~5GB 共享底座]
#   ── 各服务 = base + 业务 diff（本仓库） ──
#   tm-lite        rag-base-lite + scripts/src/ui + app ENV/EXPOSE/HC/ENTRY   [由旧 lite 改 FROM]
#   tm-full        rag-base      + scripts/src/ui + app ENV/EXPOSE/HC/ENTRY   [由旧 full 改 FROM]
#
# Single source of truth for Python deps is pyproject.toml. constraints.txt
# pins versions that pip would otherwise resolve in a way the runtime can't
# support (notably the headless variants of opencv).
#
# Per repo R1: this Dockerfile is built only by CI or local buildx. The
# remote production host never builds — it only `docker pull`s the image.
#
# OSS 自包含：所有阶段都在本文件内，`docker build --target tm-full` 可完全自建，
# 不依赖任何已发布的 ghcr base。已发布 rag-base 仅为省盘/省时的叠加便利层（§6）。
# =============================================================================

ARG PYTHON_VERSION=3.13
ARG PYTHON_IMAGE=python:${PYTHON_VERSION}-slim-bookworm
ARG TM_VERSION=dev
ARG TM_SOURCE_REV=dev

# -----------------------------------------------------------------------------
# Stage: ui-builder  — produces /app/static/admin (the Vite-built React SPA).
# [不变] Decoupled from deps/ so a Python-only change doesn't invalidate the JS
# install cache, and a UI-only change doesn't pay the pip resolver cost.
# -----------------------------------------------------------------------------
FROM node:20-alpine AS ui-builder
WORKDIR /ui
# Manifest layer cached against pnpm-lock.yaml: dep install only re-runs when
# the lockfile actually changes.
COPY dashboard/package.json dashboard/pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile --prod=false
COPY dashboard/ ./
RUN pnpm build
# Output: /ui/dist/ → copied verbatim into runtime stages as /app/static/admin.

# -----------------------------------------------------------------------------
# Stage: deps  — resolve and install runtime Python deps. Cached aggressively
# [不变] because we only re-execute when pyproject.toml or constraints.txt change.
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ARG PYTHON_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CONSTRAINT=/build/constraints.txt
WORKDIR /build

# Copy only the dep manifests; src/ + README content arrive later in service
# stages so doc/code edits don't invalidate this expensive layer.
# A stub README + minimal __init__ are enough for hatchling to resolve the
# project metadata without forcing a rebuild on every README touch.
COPY pyproject.toml constraints.txt ./
RUN echo "stub for build-time metadata only" > README.md \
    && mkdir -p src/tm_server \
    && echo '__version__ = "0.0.0-build"' > src/tm_server/__init__.py

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt .

# -----------------------------------------------------------------------------
# Stage: deps-full — add multimodal extras under the same constraints, then
# [不变] pre-warm mineru's model cache so the first /documents/file request
# doesn't stall on a multi-hundred-MB download. Failure is tolerated (network
# blips in CI) — runtime falls back to lazy download.
#
# 注：本 v2 分层下 deps-full 仍保留（mineru cache 的来源），但 tm-full 不再从这里
# COPY 整个 site-packages —— 多模态包改由 rag-base 用 `pip install` 纯增量叠加
# （见 rag-base 阶段说明）。deps-full 唯一被下游 COPY 的产物是 /root/.cache/mineru。
# -----------------------------------------------------------------------------
FROM deps AS deps-full
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt ".[multimodal]"

# Always create the cache dir so the downstream COPY succeeds even when the
# pre-warm step is a no-op (e.g. when mineru's API surface changes).
# A populated cache makes the first /documents/file response fast; an empty
# dir is safe — mineru will lazy-download at first use.
RUN mkdir -p /root/.cache/mineru \
    && (python -c "from mineru.cli.common import prepare_env; prepare_env()" 2>/dev/null \
        || python -c "import mineru" 2>/dev/null \
        || echo "mineru pre-warm skipped — will lazy-download at first use") \
    && ls -la /root/.cache/mineru

# =============================================================================
# 通用基础层（无业务代码，发布到 ghcr.io/leekkk2/rag-base{,-lite}）
# =============================================================================

# -----------------------------------------------------------------------------
# Stage: rag-sys-base — OS libs + non-root user + /data ONLY.
# [由旧 runtime-base 拆出 · 核心改动]
#   改了什么：相比旧 runtime-base，移除了 `COPY scripts/ src/`、`WORKDIR /app`、
#             所有 app 级 ENV(WORKSPACE/PYTHONPATH/PATH/...)、EXPOSE、HEALTHCHECK、
#             ENTRYPOINT，以及 tm-server 专属的 OCI LABEL。
#   为何：base 必须服务无关（service-agnostic）。剥离业务代码后，memory-app-server
#         及未来 RAG 服务都能 `FROM ghcr.io/leekkk2/rag-base` 直接复用同一份重底座，
#         eva 上 Docker 层去重 → ~5GB 只存一份。app 配置全部下沉到各服务的 tm-* 阶段。
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS rag-sys-base
ARG PYTHON_VERSION

# 通用 base 的 OCI 标注：标的是 rag-base 这个共享基础镜像本身，不绑定 tm-server。
# 服务级 title/version 标注在 tm-* 阶段补。
LABEL org.opencontainers.image.title="rag-base" \
      org.opencontainers.image.description="Shared multimodal RAG base (OS libs + Python multimodal deps + mineru cache, no app code)" \
      org.opencontainers.image.source="https://github.com/leekkk2/transcendence-memory-server" \
      org.opencontainers.image.licenses="MIT"

# Runtime system deps（与旧 runtime-base 完全一致，未增删）：
#   libgl1 / libglib2.0-0 / libgomp1   opencv-headless + mineru
#   poppler-utils                       mineru PDF text extraction
#   libmagic1                           python-magic file-type sniffing
#   gosu                                drop-privilege launcher used by entrypoint
# No curl — healthcheck is a stdlib Python script (scripts/healthcheck.py),
# 该脚本由服务阶段 COPY 进来，base 不含。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        libmagic1 \
        gosu \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID 10001 picked above default-system range to stay clear of
# host system accounts when bind-mounting host paths.
RUN groupadd --system --gid 10001 tm \
    && useradd --system --uid 10001 --gid tm --home-dir /home/tm \
               --create-home --shell /usr/sbin/nologin tm

# Pre-create /data with correct ownership. Bind-mounted volumes override
# this, but the chown gives sane defaults when the volume is empty.
RUN install -d -o tm -g tm /data /data/tasks /data/memory /data/memory_archive

# 注意：此阶段刻意不设 WORKDIR /app / 不 COPY 代码 / 不设 ENTRYPOINT。
# base 是"会运行的 OS+依赖底座"，但"怎么跑哪个服务"由 tm-* 阶段决定。

# -----------------------------------------------------------------------------
# Stage: rag-base-lite — rag-sys-base + deps site-packages（无多模态、无业务代码）。
# [新增 · 可发布到 ghcr.io/leekkk2/rag-base-lite]
#   改了什么：把原本散落在旧 lite 阶段里的 `COPY --from=deps site-packages` + uvicorn
#             bin 提取为一个独立的、无代码的可发布层。
#   为何：① 给只需轻量（无本地解析）的服务/MVP 一个共享的瘦 base；② 作为 rag-base 的
#         父层 —— rag-base 在它之上"叠加"多模态增量，从而保证 full 的公共部分与 lite
#         共享同一 blob（Docker 去重），实现 `rag-base = rag-base-lite + 纯多模态 diff`。
# -----------------------------------------------------------------------------
FROM rag-sys-base AS rag-base-lite
ARG PYTHON_VERSION
COPY --from=deps /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                 /usr/local/lib/python${PYTHON_VERSION}/site-packages
# Selective bin copy — only entry points we actually invoke from runtime.
COPY --from=deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# -----------------------------------------------------------------------------
# Stage: rag-base — rag-base-lite + .[multimodal] 纯增量 + mineru cache。★~5GB 共享底座。
# [新增 · 可发布到 ghcr.io/leekkk2/rag-base · 主 spec §3 方式 A]
#   改了什么：相比旧 full 的 `COPY --from=deps-full 整个 site-packages`，这里改为
#             `FROM rag-base-lite` 之上 `RUN pip install ".[multimodal]"`，让多模态包
#             叠加在 base-lite 已有 site-packages 之上，生成一个"只新增多模态包"的增量层。
#   为何用 pip install 叠加、而非 COPY 整个 site-packages（关键决策）：
#     · COPY 整个 deps-full/site-packages 会把"基础包 + 多模态包"揉进同一个新 blob，
#       与 rag-base-lite 的 site-packages 层是不同的 blob → Docker 不去重公共部分，
#       full 与 lite 各占一份完整 site-packages，违背"base+diff 去重"目标（这正是旧
#       Dockerfile L177-178 的结构性缺陷）。
#     · `FROM rag-base-lite; RUN pip install` 则把多模态包**写在 base-lite 之上的新层**，
#       该层 diff 仅含 torch/mineru/opencv 等新增文件；base-lite 层原样共享。这才是真正
#       的 `base + diff`。
#   纯增量的前提（务必守）：constraints.txt 锁版本，保证 .[multimodal] extra 只新增包、
#     不回改 base-lite 已装包的版本（否则 pip 会卸载/重装基础包，diff 层夹带 churn、
#     体积虚高、去重失效）。CI 用 docker history 断言体积（见 §4）。
# -----------------------------------------------------------------------------
FROM rag-base-lite AS rag-base
ARG PYTHON_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
# 复用同一份 pyproject.toml + constraints.txt 解析 .[multimodal]。stub README/__init__
# 仅供 hatchling 解析 metadata，与 deps 阶段同理，不引入业务代码。
COPY pyproject.toml constraints.txt ./
RUN echo "stub for build-time metadata only" > README.md \
    && mkdir -p src/tm_server \
    && echo '__version__ = "0.0.0-build"' > src/tm_server/__init__.py
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt ".[multimodal]"
# 清掉 build stub，避免污染 base（base 必须无业务代码痕迹）。
RUN rm -rf /build

# mineru pre-warm cache —— 唯一仍从 deps-full COPY 的产物。放进 base 的 tm 用户家目录，
# --chown 到 tm 以便非特权运行用户可读。这是 ~数百 MB 的模型缓存，属于"重底座"的一部分。
COPY --from=deps-full --chown=tm:tm /root/.cache/mineru /home/tm/.cache/mineru

# =============================================================================
# 各服务最终镜像 = base + 业务 diff（本仓库 tm-server）
# 仅这两个阶段含业务代码 + app 级配置；推到 transcendence-memory-server:<ver>-{lite,full}。
# =============================================================================

# -----------------------------------------------------------------------------
# Stage: tm-lite — final lite image = rag-base-lite + tm-server 代码 + app 配置。
# [由旧 lite 改造]
#   改了什么：① FROM 从 runtime-base 改为 rag-base-lite；② 不再 COPY site-packages
#             （已在 base-lite）；③ 把旧 runtime-base 里的 app 级 ENV/EXPOSE/
#             HEALTHCHECK/ENTRYPOINT 下沉到这里；④ 仅保留薄代码层（scripts/src/ui）。
#   为何：服务专属配置（端口 8711、healthcheck 脚本、entrypoint、PYTHONPATH）属于
#         "服务"而非"通用 base"，必须落在服务阶段；代码层放最上层 → docker history
#         末尾几层即 MB 级薄 diff。
# -----------------------------------------------------------------------------
FROM rag-base-lite AS tm-lite
ARG PYTHON_VERSION
ARG TM_VERSION
ARG TM_SOURCE_REV

LABEL org.opencontainers.image.title="transcendence-memory-server" \
      org.opencontainers.image.version="${TM_VERSION}" \
      org.opencontainers.image.revision="${TM_SOURCE_REV}" \
      org.opencontainers.image.source="https://github.com/leekkk2/transcendence-memory-server" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
ENV TM_BUILD_FLAVOR=lite \
    WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/scripts:/app/src \
    PATH="/app/scripts:${PATH}" \
    TM_RUN_AS_UID=10001 \
    TM_RUN_AS_GID=10001

# 薄代码层（放最上，最易变 → 最大化下层缓存命中）。
# .tm-source-rev 让此层依赖 commit SHA，防止远端 build cache 在仅 bump 元数据的
# release tag 后还提供旧 /app/scripts。
RUN printf '%s\n' "$TM_SOURCE_REV" > /app/.tm-source-rev
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
# Admin dashboard bundle (Vite output)；FastAPI 启动时检测并挂载到 /admin/ui。
COPY --from=ui-builder --chown=tm:tm /ui/dist /app/static/admin
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py

EXPOSE 8711
# Healthcheck uses Python stdlib (no curl). start-period 宽松，避首启 mineru import 误判。
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD ["python3", "/app/scripts/healthcheck.py"]
# 容器以 root 起，entrypoint.sh chown /data 后 gosu 降权到 UID 10001 再 exec uvicorn。
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# -----------------------------------------------------------------------------
# Stage: tm-full — final full image = rag-base + tm-server 代码 + app 配置。
# [由旧 full 改造]
#   改了什么：① FROM 从 runtime-base 改为 rag-base（已含多模态包 + mineru cache）；
#             ② 不再 COPY deps-full 整个 site-packages、不再 COPY mineru cache
#             （都已在 rag-base）；③ app 配置下沉至此；④ 仅薄代码层。
#   为何：tm-full = rag-base + 薄 diff —— 与 tm-lite 共享 rag-base-lite 底座，与 base
#         共享多模态层，eva 上不再为每个 full 各存一份 ~5GB。start-period 给足 full
#         冷启动（mineru import + lightrag init）。
# -----------------------------------------------------------------------------
FROM rag-base AS tm-full
ARG PYTHON_VERSION
ARG TM_VERSION
ARG TM_SOURCE_REV

LABEL org.opencontainers.image.title="transcendence-memory-server" \
      org.opencontainers.image.version="${TM_VERSION}" \
      org.opencontainers.image.revision="${TM_SOURCE_REV}" \
      org.opencontainers.image.source="https://github.com/leekkk2/transcendence-memory-server" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
ENV TM_BUILD_FLAVOR=full \
    WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/scripts:/app/src \
    PATH="/app/scripts:${PATH}" \
    TM_RUN_AS_UID=10001 \
    TM_RUN_AS_GID=10001

RUN printf '%s\n' "$TM_SOURCE_REV" > /app/.tm-source-rev
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
COPY --from=ui-builder --chown=tm:tm /ui/dist /app/static/admin
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py

EXPOSE 8711
# start-period 比 lite 更长：full 冷启动要 import mineru / 初始化 lightrag，首 /health 可能 >60s。
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD ["python3", "/app/scripts/healthcheck.py"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
```

### 1.1 注意点（粘贴前自查）

1. **mineru cache 路径**：旧 `full` 阶段把 cache COPY 到 `/home/tm/.cache/mineru`。新结构里这步上移到 `rag-base`，路径不变（`/home/tm/.cache/mineru`），运行用户 `tm`（UID 10001）家目录可读，行为等价。
2. **uvicorn bin**：旧 `lite`/`full` 各自 `COPY --from=deps(-full) /usr/local/bin/uvicorn`。新结构里只在 `rag-base-lite` COPY 一次，`rag-base` 与 `tm-*` 继承，无需重复。
3. **`WORKDIR /build` 残留**：`rag-base` 阶段 `pip install` 后 `RUN rm -rf /build` 清掉 stub，确保 base 无业务代码痕迹；之后 `tm-*` 阶段重设 `WORKDIR /app`。
4. **`deps-full` 仍保留**：它现在唯一对下游有用的产物是 `/root/.cache/mineru`（被 `rag-base` COPY）。其 site-packages 不再被任何阶段整体 COPY —— 多模态包改由 `rag-base` 的 `pip install` 重新解析装在 base-lite 之上。两者用同一 `constraints.txt`，版本一致。
   - 可选优化（非本次必做）：若想省 `deps-full` 的 site-packages 构建开销，可把 `deps-full` 瘦身为"只装 mineru 并预热 cache"。本附录保守保留原 `deps-full` 不动，降低改动面。

---

## 2. CI 发布（GitHub Actions）

目标：在现有 `ci.yml`（Docker Hub 发服务镜像）基础上，**新增** GHCR 发布 `rag-base{,-lite}` 的能力，并把服务镜像构建的 `--target` 由 `lite`/`full` 改名为 `tm-lite`/`tm-full`。

### 2.1 阶段名改名（必须，否则 CI 找不到 target）

`ci.yml` 现有两处 `target: ${{ matrix.flavor }}`（`docker-validate` L60、`publish-docker` L183），`matrix.flavor ∈ {lite, full}`。新 Dockerfile 把服务阶段改名为 `tm-lite`/`tm-full`，因此 target 表达式改为加前缀：

```diff
# docker-validate job
       - name: Build ${{ matrix.flavor }} image (${{ matrix.platform }})
         uses: docker/build-push-action@v5
         with:
           context: .
-          target: ${{ matrix.flavor }}
+          target: tm-${{ matrix.flavor }}
           platforms: ${{ matrix.platform }}
```

```diff
# publish-docker job
       - name: Build and push by digest
         id: build
         uses: docker/build-push-action@v5
         with:
           context: .
-          target: ${{ matrix.flavor }}
+          target: tm-${{ matrix.flavor }}
           platforms: ${{ matrix.platform }}
```

> `merge-manifests` 仍产出 `transcendence-memory-server:<ver>-{lite,full}`（tag 名不带 `tm-` 前缀），compose / deploy `.env` 的 `TM_IMAGE` 引用不变。改名只影响 Dockerfile 内部 `--target`。

### 2.2 新增 job：`publish-rag-base`（推 GHCR）

在 `ci.yml` 增加一个独立 job，仅在 tag push 时触发，buildx 多 arch 直接推 GHCR。base 与服务**版本解耦**（见 §2.4），所以单独成 job、用独立的 base 版本变量，不复用 `${GITHUB_REF_NAME}`。

```yaml
  # ---------------------------------------------------------------------------
  # publish-rag-base — 发布通用共享基础镜像到 GHCR（公开）。
  #   · 只在 tag push 触发；base 版本由 RAG_BASE_VERSION 决定（与服务 tag 解耦）。
  #   · 用 GITHUB_TOKEN（需 packages: write）登录 ghcr.io，无需额外 secret。
  #   · 多 arch 单步 push（base 改动频率低、可容忍 QEMU；如需提速可仿 publish-docker
  #     拆 per-arch + merge-manifests，本版先用简单形态）。
  #   · 推两个 target：rag-base-lite → ghcr.io/leekkk2/rag-base-lite，
  #                    rag-base      → ghcr.io/leekkk2/rag-base。
  # ---------------------------------------------------------------------------
  publish-rag-base:
    needs: [test]
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write          # ← 关键：GITHUB_TOKEN 默认只读 packages，必须显式提权
    env:
      # base 版本与服务版本解耦：手动维护。命名见 §2.4。
      RAG_BASE_VERSION: "1.3-py3.13"
      RAG_BASE_REPO: ghcr.io/leekkk2/rag-base
      RAG_BASE_LITE_REPO: ghcr.io/leekkk2/rag-base-lite
    strategy:
      fail-fast: false
      matrix:
        include:
          - target: rag-base-lite
            repo_var: RAG_BASE_LITE_REPO
          - target: rag-base
            repo_var: RAG_BASE_REPO
    steps:
      - uses: actions/checkout@v4
      # rag-base 多模态 target 在 GHA runner 上盘紧 → 与 publish-docker 同款清盘。
      - name: Free up disk space (rag-base only)
        if: matrix.target == 'rag-base'
        run: |
          df -h /
          sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc \
            /opt/hostedtoolcache/CodeQL "$AGENT_TOOLSDIRECTORY"/PyPy \
            "$AGENT_TOOLSDIRECTORY"/Ruby "$AGENT_TOOLSDIRECTORY"/go || true
          sudo docker image prune --all --force || true
          df -h /
      - uses: docker/setup-qemu-action@v3      # 多 arch 单步 push 需要 QEMU
      - uses: docker/setup-buildx-action@v3
      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}   # packages:write 由上面 permissions 提供
      - name: Build & push ${{ matrix.target }}
        uses: docker/build-push-action@v5
        with:
          context: .
          target: ${{ matrix.target }}
          platforms: linux/amd64,linux/arm64
          push: true
          tags: |
            ${{ env[matrix.repo_var] }}:${{ env.RAG_BASE_VERSION }}
          # base 层用独立 cache scope，与服务镜像 cache 隔离。
          cache-from: type=gha,scope=ragbase-${{ matrix.target }}
          cache-to: type=gha,scope=ragbase-${{ matrix.target }},mode=max
          build-args: |
            TM_VERSION=${{ github.ref_name }}
            TM_SOURCE_REV=${{ github.sha }}
```

要点说明：
- **`packages: write` 权限**：GitHub Actions 的 `GITHUB_TOKEN` 默认对 packages 只读；推 GHCR 必须在 job 级 `permissions: { packages: write }` 显式提权。无需创建 PAT/额外 secret。首次推送后，去 GHCR package 设置把 `rag-base` / `rag-base-lite` 的可见性设为 **Public**（满足"谁都能 pull"），并在 package settings 里把本仓库加为 linked repo 以便后续 `GITHUB_TOKEN` 持续有权 push。
- **`leekkk2` 大小写**：GHCR namespace 必须全小写；`github.actor` 在 push 场景即仓库 owner（`leekkk2`），与 `env[matrix.repo_var]` 里硬编码的小写 `leekkk2` 一致。
- **多 arch 形态**：这里用 `setup-qemu-action` + 单步多平台 push（简单）。base 镜像改动频率远低于服务，QEMU 慢一点可接受。若后续 base 构建超时，再仿 `publish-docker` 的 per-arch 原生 runner + push-by-digest + merge-manifests 拆分。

### 2.3 服务镜像如何"复用已发布 base"（可选优化，非必须）

`publish-docker` 仍走"自包含自建"路径（`docker build --target tm-full` 把 `rag-base*` 阶段在本仓库内重建）——**这是 OSS 自包含的保证，默认保留**。

若想让服务 CI 直接 `FROM` 已发布的 GHCR base 以省构建时间，可在 Dockerfile 顶部加一个可切换的 ARG（**进阶，非本次必做**）：

```dockerfile
# 默认本地自建 base（OSS 自包含）；CI 可传 --build-arg RAG_BASE_REF=ghcr.io/leekkk2/rag-base:1.3-py3.13
# 来跳过 base 重建。注意：此法需把 tm-full 的 FROM 改成 ARG，会牺牲单文件 `--target tm-full`
# 的纯自包含性，故仅在 CI 显式开启，本地/OSS 构建保持默认空值走自建。
```

> 结论：**默认不开启**此优化。`publish-docker` 继续自建服务镜像（buildx GHA cache 已让 base 层命中复用），既快又保持自包含。`publish-rag-base` 只负责把 base 单独发到 GHCR 供 memory-app / 未来服务 `FROM`。

### 2.4 版本 / tag 策略

| 镜像 | tag 形态 | 取值来源 | 说明 |
|---|---|---|---|
| `ghcr.io/leekkk2/rag-base` | `1.3-py3.13` | `RAG_BASE_VERSION`（手维护） | 与服务版本**解耦**。`1.3` 跟随 raganything 主版本，`py3.13` 跟随 `ARG PYTHON_VERSION`。 |
| `ghcr.io/leekkk2/rag-base-lite` | `1.3-py3.13` | 同上 | 与 `rag-base` 同版本节奏（同一 base-lite 父层）。 |
| `transcendence-memory-server` (Docker Hub) | `<ver>-lite` / `<ver>-full` / `latest` / `lite` / `full` | `${GITHUB_REF_NAME#v}` | **不变**，沿用现有 `merge-manifests`。 |

- 升级策略：base 与服务**独立打 tag**。base 升级（如 raganything 1.3→1.4 或 py3.13→3.14）时 bump `RAG_BASE_VERSION`，在 release notes 关联记录"本次 `transcendence-memory-server:<ver>` 基于 `rag-base:<base-ver>`"。
- 消费侧 pin：memory-app-server 的 Dockerfile 用 `FROM ghcr.io/leekkk2/rag-base:1.3-py3.13` **显式 pin**，base 升级时各服务主动重建（Podfile 式声明依赖）。
- 可选日历版：也可叠加 `rag-base:2026.06` 滚动 tag，但生产/消费侧务必 pin 精确版（`1.3-py3.13`），避免漂移。

---

## 3. OSS 自包含验证

主 spec §6 的硬约束：**Dockerfile 内含全部阶段，`docker build --target tm-full` 不依赖任何已发布镜像即可自建**。验证步骤：

```bash
cd transcendence-memory-server

# 3.1 自包含自建 tm-full（不预拉任何 ghcr base，纯本地从 python-slim 起）
docker build --target tm-full -t tm:selfcontained-full .
# 期望：成功。证明 rag-sys-base→rag-base-lite→rag-base→tm-full 全链在本文件内闭合。

# 3.2 自建 rag-base 并校验"base + diff"分层
docker build --target rag-base -t rag-base:test .
docker build --target rag-base-lite -t rag-base-lite:test .

# docker history：确认 rag-base 是在 rag-base-lite 之上叠加多模态增量层
docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' rag-base:test
#   期望从下往上读：
#     · OS apt 层 + useradd + install /data   （rag-sys-base，几十~百 MB）
#     · COPY deps site-packages               （rag-base-lite，~数百 MB）
#     · RUN pip install .[multimodal]         （rag-base 增量，~GB 级 ← 多模态 diff 在独立层）
#     · COPY mineru cache                      （rag-base，数百 MB）

# 3.3 tm-full 末尾只新增薄代码层
docker history --format '{{.Size}}\t{{.CreatedBy}}' tm:selfcontained-full | head -n 8
#   期望最上面几层：COPY scripts/ + COPY src/ + COPY ui dist + chmod + .tm-source-rev
#   每层 KB~MB 级（薄 diff），不含任何 GB 级 site-packages COPY。

# 3.4 运行冒烟（full flavor + 一次 PDF 摄取走 mineru）
docker run -d --name tm-full-smoke -p 8711:8711 tm:selfcontained-full
sleep 60   # full 冷启动给足 mineru import
curl -s localhost:8711/health | python3 -m json.tool   # 期望 build_flavor=full
# 上传一个 PDF 走 /documents/file，确认 mineru 本地解析链路通（按服务实际鉴权头补 token）
docker rm -f tm-full-smoke
```

通过判据：3.1 自建成功（不 pull 任何外部 base）；3.2 `rag-base` 的多模态层与 mineru cache 层在 `rag-base-lite` 之上独立可见；3.3 `tm-full` 顶部只有 MB 级代码层；3.4 `/health` 返回 `full` 且 PDF 摄取成功。

---

## 4. 风险与体积断言

### 4.1 diff 纯增量是"待验证假设"，靠 §4.2 体积断言兜底（constraints 锁面窄）

> ⚠️ 修正（2026-06-01 审计）：早前措辞"constraints 锁版本**保证** diff 纯增量"过强。实查 `constraints.txt`：只 exact-pin 两个包 `opencv-python-headless==4.10.0.84` / `opencv-contrib-python-headless==4.10.0.84`；`pyarrow` 仅 `>=15` 下界；`numpy` **故意不 pin**（注释："numpy is intentionally NOT pinned"）。所以"纯增量"**不是** constraints 的强保证，而是一个**待验证假设**——只有 `.[multimodal]` extra 恰好不要求改 numpy/pyarrow 时才成立。真正兜底靠 §4.2 的 docker-history 体积断言（已升为 Phase 1 必做门禁），不可仅凭 constraints 假定已锁死。

- **风险**：`rag-base` 的 `pip install ".[multimodal]"` 若未受约束，pip 可能**回改** `rag-base-lite` 已装基础包（升级/降级 numpy、pyarrow 等），导致 diff 层不仅"新增多模态包"还"重写基础包" → 层体积虚高、与 base-lite 公共部分不再共享 blob、去重失效。
- **缓解（部分）**：`rag-base` 阶段 `pip install` 强制带 `--constraint constraints.txt`（与 `deps`/`deps-full` 同一份约束文件，§1 L227 已写 `pip install --constraint constraints.txt ".[multimodal]"`，OK）。但该文件仅 pin opencv-headless 两包、`pyarrow>=15` 下界、`numpy` 不 pin —— 它只能保证 base-lite 与 rag-base 对**已 pin 的包**解析一致，**无法**阻止 multimodal extra 引入更高/更低的 numpy/pyarrow 把 base-lite 已装版本顶掉。注释虽写"Bump versions deliberately, not by accident"，但未 pin 的包不受此保护。
- **附加保险**：保持 `deps` 与 `rag-base` 用同一 `pyproject.toml` + `constraints.txt`，两者对**已锁定包**的解析结果天然一致；但 numpy/pyarrow 这类未锁包的最终一致性，仍须由 §4.2 体积断言实测把关，发现 churn（diff 异常膨胀）即让 CI 红。

### 4.2 docker history 体积断言（Phase 1 必做 CI 门禁，非"建议"）

> ⚠️ 本断言由"建议"升级为 **Phase 1 必做 CI 门禁**。原因（见 §4.1 修正）：`constraints.txt` 锁面很窄——只 exact-pin 两个 opencv-headless（`==4.10.0.84`），`numpy` 故意不 pin、`pyarrow` 仅 `>=15` 下界。因此"多模态 diff 纯增量"不是 constraints 的强保证，而是一个**待验证假设**（只有 multimodal extra 恰好不触发 numpy/pyarrow 改版时才成立）。这条 docker-history 体积断言是"去重收益（~5GB）不被 numpy/pyarrow churn 悄悄打折"的**唯一保障**，必须在 `publish-rag-base` job 内强制执行、失败即让 CI 红。

为防止"diff 夹带 churn"悄悄退化，在 `publish-rag-base` job **强制**加一个轻量断言 step（构建本地 `rag-base` / `rag-base-lite` 后比对）：

```yaml
      - name: Assert rag-base diff stays incremental
        if: matrix.target == 'rag-base'
        run: |
          set -euo pipefail
          # 本地各建一个用于体积比对（buildx 缓存命中，开销小）。
          docker build --target rag-base-lite -t _ragbl:assert --load .
          docker build --target rag-base      -t _ragb:assert  --load .
          lite_b=$(docker image inspect _ragbl:assert --format '{{.Size}}')
          full_b=$(docker image inspect _ragb:assert  --format '{{.Size}}')
          diff_gb=$(awk -v a="$full_b" -v b="$lite_b" 'BEGIN{printf "%.2f",(a-b)/1024/1024/1024}')
          echo "rag-base-lite=$((lite_b/1024/1024))MB  rag-base=$((full_b/1024/1024))MB  multimodal_diff=${diff_gb}GB"
          # 断言：lite ≤ 1.2GB；多模态增量 diff 在合理区间（torch+opencv+mineru ~3.5-5.5GB）。
          # 上界用于捕获"diff 夹带 base 包 churn 导致虚胖"。阈值随依赖演进调。
          awk -v l="$lite_b" 'BEGIN{ if (l > 1.2*1024*1024*1024){ print "FAIL: rag-base-lite >1.2GB"; exit 1 } }'
          awk -v d="$diff_gb" 'BEGIN{ if (d+0 > 6.0){ print "FAIL: multimodal diff >6GB (churn?)"; exit 1 }
                                       if (d+0 < 2.0){ print "WARN: multimodal diff <2GB (multimodal missing?)" } }'
```

> 断言数值是经验上界，随 torch/mineru 版本演进调整；核心目的是当某次 bump 让 diff 异常膨胀（churn 信号）时**让 CI 红**，而非静默退化。

### 4.3 其他风险（承接主 spec §8）

- **base/diff 版本偏移**：消费侧（memory-app）pin 精确 `rag-base:1.3-py3.13`；base 升级走 release + 通知两服务重建。本仓库自身因 `publish-docker` 自建服务镜像，不受 GHCR base tag 影响。
- **回滚**：保留旧 `transcendence-memory-server:<ver>-full`（自包含整镜像，可直接 `docker pull` 回退）；或 `git revert` 本 Dockerfile 改动恢复旧 `runtime-base`/`lite`/`full` 结构。
- **eva 实施铁律**（部署消费侧时务必守，见主 spec §8 + 消费侧 spec §C）：
  - 拉/建多 GB 镜像前先清磁盘（`docker image prune` + 旧 image/backup 迁 rclone），eva 当前 ~94% 占用。
  - env/镜像变更 recreate 一律 `docker compose up -d --force-recreate --pull never`（防 `TM_IMAGE` tag 漂移去拉 GB 镜像撑爆盘）。
  - recreate 前 `docker inspect <c> --format '{{.Config.Image}}'` 对比 compose 声明 tag，不一致先对齐（0.17.2 vs 0.17.3 撑爆盘教训）。
  - 重建后核 `getent hosts new-api`（app 容器须能解析 new-api；eva 有 `~/bin/ensure-newapi-networks.sh` + 5min cron 自愈）。
  - 本次镜像升级**不动 newapi key**（memory-app/tm-server 已用 prod-memory-app/prod-tm token）。
```
