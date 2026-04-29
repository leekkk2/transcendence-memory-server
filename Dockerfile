# syntax=docker/dockerfile:1.7
# ===================================================================
# Multi-stage Dockerfile for transcendence-memory-server
# Stages:
#   builder-base   - 安装核心 Python 依赖（共享层，缓存最大化）
#   builder-lite   - lite flavor（无 multimodal 编译）
#   builder-full   - full flavor（含 mineru / raganything / opencv-headless）
#   runtime-base   - 系统包 + 应用代码 + healthcheck（runtime 公共层）
#   lite / full    - 最终 image，仅复制对应 builder 的 site-packages
#
# 构建：远端禁用，统一用 GitHub Actions 或本地 buildx
# 见仓库根 CLAUDE.md 的 R1-R5 规则
# ===================================================================

ARG PYTHON_IMAGE=python:3.13-slim

# ---------- builder-base：核心依赖 ----------
FROM ${PYTHON_IMAGE} AS builder-base
WORKDIR /build
# 1. 先复制 metadata，让依赖层独立于源码变化
COPY pyproject.toml README.md ./
# 2. 再装通用依赖（pyproject 里没声明的运行时包）
RUN pip install --no-cache-dir \
        fastapi uvicorn httpx requests numpy \
        lancedb pyarrow lightrag-hku
# 3. 最后复制源码并安装本项目
COPY src/ ./src/
RUN pip install --no-cache-dir .

# ---------- builder-lite：lite flavor 等价 builder-base ----------
FROM builder-base AS builder-lite

# ---------- builder-full：multimodal flavor ----------
# 合并多个 RUN：装 multimodal extras，然后强制把 opencv-python 替换为 headless
# （RAGAnything 上游会拉 GUI 版的 opencv-python，import cv2 会要求 libxcb 等 X11 库）
FROM builder-base AS builder-full
RUN pip install --no-cache-dir ".[multimodal]" \
    && pip uninstall -y opencv-python opencv-contrib-python || true \
    && pip install --no-cache-dir --force-reinstall opencv-python-headless

# ---------- runtime-base：系统包 + 应用代码 ----------
FROM ${PYTHON_IMAGE} AS runtime-base
# 系统依赖：
#   curl              -> healthcheck
#   libgl1 / libglib2.0-0 / libgomp1  -> opencv-headless & mineru
#   poppler-utils     -> mineru PDF 处理
#   libmagic1         -> python-magic 文件类型嗅探
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# 应用代码：scripts 在前（运行时入口），src 在后（被 site-packages 安装版本覆盖）
COPY --chmod=755 scripts/ ./scripts/
COPY src/ ./src/

ENV WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8711
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD curl -f http://localhost:8711/health || exit 1
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# ---------- lite ----------
FROM runtime-base AS lite
COPY --from=builder-lite /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder-lite /usr/local/bin /usr/local/bin
ENV TM_BUILD_FLAVOR=lite

# ---------- full ----------
FROM runtime-base AS full
COPY --from=builder-full /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder-full /usr/local/bin /usr/local/bin
ENV TM_BUILD_FLAVOR=full
