FROM python:3.13-slim AS builder
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
RUN pip install --no-cache-dir \
    fastapi uvicorn httpx requests numpy \
    lancedb pyarrow lightrag-hku
# raganything 从 GitHub 安装（可选，失败不阻塞构建）
RUN pip install --no-cache-dir git+https://github.com/HKUDS/RAG-Anything.git || true

FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY scripts/ ./scripts/
COPY src/ ./src/
COPY pyproject.toml README.md ./
RUN chmod +x /app/scripts/entrypoint.sh
RUN mkdir -p /data/tasks/active /data/tasks/archived /data/tasks/rag/containers /data/memory /data/memory_archive
ENV WORKSPACE=/data
EXPOSE 8711
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s CMD curl -f http://localhost:8711/health || exit 1
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
