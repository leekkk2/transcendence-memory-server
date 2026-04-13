FROM python:3.13-slim AS builder-base
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx requests numpy \
    lancedb pyarrow lightrag-hku

FROM builder-base AS builder-lite

FROM builder-base AS builder-full
RUN pip install --no-cache-dir ".[multimodal]"
# RAGAnything 依赖 mineru → cv2。上游把 opencv-python (GUI 版) 也装了进来，
# 会在 import cv2 时要求 libxcb 等 X11 库；强制只保留 headless 版本即可。
RUN pip uninstall -y opencv-python opencv-contrib-python || true \
    && pip install --no-cache-dir --force-reinstall opencv-python-headless

FROM python:3.13-slim AS runtime-base
# curl: healthcheck
# libgl1 / libglib2.0-0 / libgomp1: opencv-python-headless & mineru 运行时必需
# poppler-utils: mineru PDF 处理
# libmagic1: python-magic 文件类型嗅探
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY pyproject.toml README.md ./
RUN chmod +x /app/scripts/entrypoint.sh
RUN mkdir -p /data/tasks/active /data/tasks/archived /data/tasks/rag/containers /data/memory /data/memory_archive
ENV WORKSPACE=/data
EXPOSE 8711
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s CMD curl -f http://localhost:8711/health || exit 1
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

FROM runtime-base AS lite
COPY --from=builder-lite /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder-lite /usr/local/bin /usr/local/bin
ENV TM_BUILD_FLAVOR=lite

FROM runtime-base AS full
COPY --from=builder-full /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder-full /usr/local/bin /usr/local/bin
ENV TM_BUILD_FLAVOR=full
