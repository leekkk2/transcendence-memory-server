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

# -----------------------------------------------------------------------------
# mineru model BAKE（真下载 + 强校验 · DR-048 离线优先）
#   背景：mineru 3.2.1 的 `mineru-models-download` 实际把模型写进 *底层 hub 的缓存*
#         （ModelScope → $MODELSCOPE_CACHE/hub；HF → $HF_HOME/hub），**不是** ~/.cache/mineru。
#         旧 pre-warm 调用已不存在的 `prepare_env()` → 静默跳过 → /home/tm/.cache/mineru 永远空
#         → 运行时懒下载（且 hybrid-auto 选 VLM 去拉不可达的 huggingface.co）→ 解析失败。
#   方案：把两个底层 hub 的缓存根都钉进 /root/.cache/mineru 子目录（下游唯一 COPY 的产物），
#         真跑 `mineru-models-download -m pipeline`（CPU pipeline 模型，DR-048 本地只跑 pipeline，
#         VLM/embedding/LLM 走 newapi），优先 ModelScope（容器内国内原生可达）、不通再 hf-mirror
#         （huggingface.co 容器内 SSL 超时，必须改道 HF_ENDPOINT=https://hf-mirror.com）。
#   强校验：下载后 cache 必须非空，**空则 build 失败**（exit 1），杜绝静默跳过再退化成懒下载。
#   注：强校验逻辑 —— ModelScope 与 HF 两个 hub 缓存任一非空即视为成功（whichever source
#       succeeded）；两者皆空 → 退出码 1，build fail。（说明不可写成 RUN 内联 # 注释：行续
#       接中夹 # 会让 shell 把后续 && 解析成空命令而语法错。）
ENV MINERU_DEVICE_MODE=cpu \
    MINERU_MODEL_SOURCE=modelscope \
    MODELSCOPE_CACHE=/root/.cache/mineru/modelscope \
    HF_HOME=/root/.cache/mineru/huggingface \
    HF_ENDPOINT=https://hf-mirror.com
RUN mkdir -p /root/.cache/mineru/modelscope /root/.cache/mineru/huggingface \
    && ( \
         echo "[mineru-bake] trying ModelScope (pipeline)..." \
         && MINERU_MODEL_SOURCE=modelscope mineru-models-download -s modelscope -m pipeline \
       ) \
    || ( \
         echo "[mineru-bake] ModelScope failed -> falling back to hf-mirror (pipeline)..." \
         && MINERU_MODEL_SOURCE=huggingface HF_ENDPOINT=https://hf-mirror.com \
            mineru-models-download -s huggingface -m pipeline \
       ) \
    && if [ -z "$(find /root/.cache/mineru -type f 2>/dev/null | head -n1)" ]; then \
           echo "MINERU_BAKE_EMPTY: model cache /root/.cache/mineru is empty after download — failing build"; \
           exit 1; \
       fi \
    && echo "[mineru-bake] cache populated ($(find /root/.cache/mineru -type f | wc -l) files, $(du -sh /root/.cache/mineru | cut -f1))" \
    && find /root/.cache/mineru -maxdepth 3 -type d | head -n 20

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
# torch 变体（OSS 可配置，默认不改变常规行为）：
#   · 默认 TORCH_INDEX_URL 为空 → 走 PyPI 常规解析（linux 即 CUDA 版），GPU 用户开箱即用，
#     与升级前行为一致，不给开源用户意外。
#   · 资源受限 / 无 GPU 主机可显式 opt-in CPU wheel，剔除 ~4GB CUDA 库（nvidia-*/triton），
#     mineru CPU 模式能力零损失：
#       docker build --target tm-full \
#         --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
#   · 本仓库 CI 的 publish-rag-base 即以 CPU 变体构建发布到 GHCR 的共享 base（见 ci.yml + DR-048
#     @ memory-app；eva 无 GPU，CUDA torch 在其上 cuda=False 纯浪费且撑大镜像）。
#   · 版本默认 2.12.0（与现行兼容 mineru[core]>=3.0.9，CPU index 有 2.12.0+cpu wheel）。
#     GPU 用户也可传 cu 专属 index（如 .../whl/cu124）。
#   · 指定 index 时变体强制力来自 ①②：torch+torchvision 从该 index 作 PRIMARY 装 + 读出实际
#     本地版本（+cpu/+cuXXX）回钉约束（仅靠 PIP_EXTRA_INDEX_URL 不足以剔默认 CUDA，详见 ① 注释）。
ARG TORCH_VERSION=2.12.0
ARG TORCH_INDEX_URL=
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
# 复用同一份 pyproject.toml + constraints.txt 解析 .[multimodal]。stub README/__init__
# 仅供 hatchling 解析 metadata，与 deps 阶段同理，不引入业务代码。
COPY pyproject.toml constraints.txt ./
RUN echo "stub for build-time metadata only" > README.md \
    && mkdir -p src/tm_server \
    && echo '__version__ = "0.0.0-build"' > src/tm_server/__init__.py
# ① 若显式指定了 torch index（CPU 或 cu* GPU wheel），先把【整个 torch 生态】
#    (torch+torchvision) 从该 index 作为 PRIMARY (--index-url) 装好，并落一份精确
#    本地版本约束文件 torch-pin.txt（钉到实际装上的 +cpu/+cuXXX 本地版本）。
#    否则（默认）写空约束文件 + 跳过预装，让 ② 的 .[multimodal] 走常规 PyPI 解析（CUDA，OSS 直觉）。
#
#   为什么必须 --index-url(primary) + 本地版本约束、而非仅 PIP_EXTRA_INDEX_URL(extra)：
#     · 同一版本 (torch 2.12.0 / torchvision) 在 PyPI(CUDA build) 与 pytorch 专属 index 都存在；
#       extra-index 只是「补充候选」，pip 对同版本不保证优先选专属 index，常落到 PyPI 的 CUDA
#       wheel → 拽入 nvidia_cudnn_cu13 等 ~4GB CUDA 库（本次 bug 根因）。
#     · 仅在 ① 预装 torch 也不够：torchvision 在 mineru[pipeline] 里【无版本约束】，② 仍会把它
#       新解析成 CUDA wheel。故 ① 必须连 torchvision 一起从指定 index 装。
#     · 本地版本（+cpu / +cu124 …）是 PEP 440 local-version 标识，只在对应 pytorch index 有 wheel；
#       从实际装上的 torch/torchvision 读出本地版本回钉 → pip 永远只能解析到该变体，二次解析无法漂移。
#       不在此硬编码 +cpu —— 否则传 cu* GPU index 时会找不到 `+cpu` wheel 而构建失败（见 onboarding §3
#       cu124 用例）。该约束仅写进 build-time 文件、不污染仓库 constraints.txt（后者由默认 GPU 的
#       deps/deps-full 共用，必须保持 GPU 友好）。
RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "${TORCH_INDEX_URL}" ]; then \
        pip install --index-url "${TORCH_INDEX_URL}" \
            "torch==${TORCH_VERSION}" torchvision; \
        T_VER="$(python -c 'import torch; print(torch.__version__)')"; \
        TV_VER="$(python -c 'import torchvision; print(torchvision.__version__)')"; \
        { echo "torch==${T_VER}"; echo "torchvision==${TV_VER}"; } > torch-pin.txt; \
    else \
        : > torch-pin.txt; \
    fi
# ② 装多模态（raganything + mineru[core]）。叠加 torch-pin.txt 第二份约束：指定 index 变体下
#    钉死 torch/torchvision 为实际装上的本地版本（+cpu / +cuXXX），阻止 .[multimodal] 把它们重解析
#    成默认 PyPI CUDA build；默认（空 index）变体下 torch-pin.txt 为空文件、无副作用。
#    PIP_EXTRA_INDEX_URL 保留以让该变体 wheel 可达（兜底）。
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_EXTRA_INDEX_URL="${TORCH_INDEX_URL}" \
    pip install --constraint constraints.txt --constraint torch-pin.txt ".[multimodal]"
# 清掉 build stub，避免污染 base（base 必须无业务代码痕迹）。
RUN rm -rf /build

# mineru pre-warm cache —— 唯一仍从 deps-full COPY 的产物。放进 base 的 tm 用户家目录，
# --chown 到 tm 以便非特权运行用户可读。这是已烤好的 pipeline 模型缓存（含 ModelScope/HF
# 两个 hub 缓存子目录），属于"重底座"的一部分。deps-full 已强校验非空，故此处 COPY 必非空。
COPY --from=deps-full --chown=tm:tm /root/.cache/mineru /home/tm/.cache/mineru

# 生成 mineru 本地模型注册表 /home/tm/mineru.json（纯离线解析的关键一步）。
#   背景（2026-06-02 `--network none` 实测纠正了原 modelscope 假设）：
#   MINERU_MODEL_SOURCE=modelscope 运行时**无视已存在缓存、仍调 ms_snapshot_download 去 ping
#   www.modelscope.cn hub API 校验** → 离线必失败（NameResolutionError）。唯一纯离线路径是
#   MINERU_MODEL_SOURCE=local：mineru 读 MINERU_TOOLS_CONFIG_JSON 指向的 mineru.json 里
#   models-dir.pipeline 直接取本地模型根、完全不联网
#   （见 mineru/utils/models_download_utils.py:auto_download_and_get_model_root_path 的 'local' 分支）。
#   官方 mineru-models-download 下载后本会调 configure_model 写 $HOME/mineru.json，但 bake 以 root 跑
#   写到 /root/mineru.json，未被上面的 COPY（只搬 .cache/mineru）带走、且路径是 /root 而非 /home/tm，
#   故必须在此按运行时真实路径重新生成。
#   FINAL repo root 取 OpenDataLab/PDF-Extract-Kit*（**排除 modelscope 下载暂存空壳 ._____temp/**——
#   实测它是 0 字节空目录，误指会让 unimernet config 缺失而解析失败）；强校验 unimernet config 存在，
#   缺失即 build 失败（杜绝指向空目录的隐性 regression）。
RUN PIPE_ROOT="$(find /home/tm/.cache/mineru/modelscope -type d -name 'PDF-Extract-Kit*' -not -path '*._____temp*' | head -n1)" \
    && if [ -z "$PIPE_ROOT" ] || [ ! -f "$PIPE_ROOT/models/MFR/unimernet_hf_small_2503/config.json" ]; then \
           echo "MINERU_LOCAL_ROOT_NOT_FOUND: pipeline 模型根或 unimernet config 缺失（PIPE_ROOT='$PIPE_ROOT'）— failing build"; \
           exit 1; \
       fi \
    && printf '{\n  "models-dir": {\n    "pipeline": "%s"\n  }\n}\n' "$PIPE_ROOT" > /home/tm/mineru.json \
    && chown tm:tm /home/tm/mineru.json \
    && echo "[mineru-local-config] /home/tm/mineru.json -> pipeline=$PIPE_ROOT" \
    && cat /home/tm/mineru.json

# 运行时强制 CPU + pipeline 后端 + 纯离线本地模型（DR-048：本地只跑 pipeline 解析，
# VLM/embedding/LLM 仍走 newapi，不本地化大模型）。所有 FROM rag-base 的服务（tm-full /
# memory-app full）统一继承，无需各自重复声明：
#   · MINERU_DEVICE_MODE=cpu        —— 关掉 cuda/mps/npu 探测，恒走 CPU pipeline。
#   · MINERU_MODEL_SOURCE=local     —— 走本地模型注册表，运行时**不 ping 任何 hub**（纯离线，`--network none` 实测 exit 0、OCR 真出文本）。
#   · MINERU_TOOLS_CONFIG_JSON      —— 绝对路径显式指向上面生成的 mineru.json（不依赖运行用户 $HOME）。
#   · MODELSCOPE_CACHE / HF_HOME / HF_ENDPOINT —— 仅作万一回退到 hub 代码路径时的兜底（local 模式下不触发）。
ENV MINERU_DEVICE_MODE=cpu \
    MINERU_MODEL_SOURCE=local \
    MINERU_TOOLS_CONFIG_JSON=/home/tm/mineru.json \
    MODELSCOPE_CACHE=/home/tm/.cache/mineru/modelscope \
    HF_HOME=/home/tm/.cache/mineru/huggingface \
    HF_ENDPOINT=https://hf-mirror.com

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
