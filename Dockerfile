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

FROM python:3.13-slim AS runtime-base
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
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
