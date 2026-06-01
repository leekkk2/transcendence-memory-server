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
# torch 变体（DR-048 · eva 实测无 GPU）：默认装 CPU wheel，剔除 ~4GB CUDA 库
# （nvidia-cuda-* + triton），mineru CPU 模式能力零损失。CUDA 版 torch 在无 GPU 主机上
# `cuda available=False`，那 ~4GB 纯浪费且把 6.23GB 镜像撑大、加剧 eva 94% 盘压力。
# GPU 部署可 build-arg 覆盖 TORCH_INDEX_URL（如 https://download.pytorch.org/whl/cu130）。
# 版本钉到 mineru[core]>=3.0.9 兼容的 2.12.0（eva 现行同版本，仅变体由 cu130 → cpu）。
ARG TORCH_VERSION=2.12.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
# 复用同一份 pyproject.toml + constraints.txt 解析 .[multimodal]。stub README/__init__
# 仅供 hatchling 解析 metadata，与 deps 阶段同理，不引入业务代码。
COPY pyproject.toml constraints.txt ./
RUN echo "stub for build-time metadata only" > README.md \
    && mkdir -p src/tm_server \
    && echo '__version__ = "0.0.0-build"' > src/tm_server/__init__.py
# ① 先从指定 index 装好 torch（默认 CPU wheel），锁住变体。这样 .[multimodal] 解析时
#    torch 已满足，pip 不会从 PyPI 默认拉 CUDA 版（PyPI linux torch 默认带 CUDA）。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url ${TORCH_INDEX_URL} "torch==${TORCH_VERSION}"
# ② 再装多模态（raganything + mineru[core]）；保留 torch index 作 extra-index，
#    确保任何 torch 二次解析仍走 CPU 变体，不夹带 CUDA churn（体积断言 §4.2 兜底）。
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_EXTRA_INDEX_URL=${TORCH_INDEX_URL} \
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
