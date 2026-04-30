# syntax=docker/dockerfile:1.7
# =============================================================================
# transcendence-memory-server — multi-stage container build
#
# Stages:
#   deps          installs runtime Python deps from pyproject.toml + constraints
#   deps-full     adds [multimodal] extras and pre-warms mineru models
#   runtime-base  shared runtime layer: system libs, non-root user, app code
#   lite          final image: deps-only site-packages
#   full          final image: deps-full site-packages + mineru model cache
#
# Single source of truth for Python deps is pyproject.toml. constraints.txt
# pins versions that pip would otherwise resolve in a way the runtime can't
# support (notably the headless variants of opencv).
#
# Per repo R1: this Dockerfile is built only by CI or local buildx. The
# remote production host never builds — it only `docker pull`s the image.
# =============================================================================

ARG PYTHON_VERSION=3.13
ARG PYTHON_IMAGE=python:${PYTHON_VERSION}-slim-bookworm
ARG TM_VERSION=dev

# -----------------------------------------------------------------------------
# Stage: deps  — resolve and install runtime Python deps. Cached aggressively
# because we only re-execute when pyproject.toml or constraints.txt change.
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps
ARG PYTHON_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_CONSTRAINT=/build/constraints.txt
WORKDIR /build

# Copy only what the dep resolver needs; source code copied later in runtime stage.
COPY pyproject.toml constraints.txt README.md ./
COPY src/tm_server/__init__.py ./src/tm_server/__init__.py

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt .

# -----------------------------------------------------------------------------
# Stage: deps-full — add multimodal extras under the same constraints, then
# pre-warm mineru's model cache so the first /documents/file request doesn't
# stall on a multi-hundred-MB download. Failure is tolerated (network blips
# in CI) — runtime falls back to lazy download.
# -----------------------------------------------------------------------------
FROM deps AS deps-full
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt ".[multimodal]"

RUN python -c "from mineru.cli.common import prepare_env; prepare_env()" 2>/dev/null \
    || python -c "import mineru" 2>/dev/null \
    || echo "mineru pre-warm skipped (will lazy-download at first use)"

# -----------------------------------------------------------------------------
# Stage: runtime-base — system libs + non-root user + app code. Final images
# inherit from this and only differ in which deps stage they copy from.
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime-base
ARG PYTHON_VERSION
ARG TM_VERSION

LABEL org.opencontainers.image.title="transcendence-memory-server" \
      org.opencontainers.image.version="${TM_VERSION}" \
      org.opencontainers.image.source="https://github.com/leekkk2/transcendence-memory-server" \
      org.opencontainers.image.licenses="MIT"

# Runtime system deps:
#   libgl1 / libglib2.0-0 / libgomp1   opencv-headless + mineru
#   poppler-utils                       mineru PDF text extraction
#   libmagic1                           python-magic file-type sniffing
# No curl — healthcheck is a stdlib Python script (scripts/healthcheck.py).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        poppler-utils \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID 10001 picked above default-system range to stay clear of
# host system accounts when bind-mounting host paths.
RUN groupadd --system --gid 10001 tm \
    && useradd --system --uid 10001 --gid tm --home-dir /home/tm \
               --create-home --shell /usr/sbin/nologin tm

# Pre-create /data with correct ownership. Bind-mounted volumes override
# this, but the chown gives sane defaults when the volume is empty.
RUN install -d -o tm -g tm /data /data/tasks /data/memory /data/memory_archive

WORKDIR /app
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py

ENV WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/scripts:/app/src \
    PATH="/app/scripts:${PATH}"

USER tm
EXPOSE 8711

# Healthcheck uses Python stdlib (no curl in image). start-period is generous
# so first-time mineru imports don't trip the probe on full flavor.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD ["python3", "/app/scripts/healthcheck.py"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# -----------------------------------------------------------------------------
# Stage: lite — final image with deps-stage site-packages only. ~700-900 MB.
# -----------------------------------------------------------------------------
FROM runtime-base AS lite
ARG PYTHON_VERSION
ENV TM_BUILD_FLAVOR=lite
COPY --from=deps /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                 /usr/local/lib/python${PYTHON_VERSION}/site-packages
# Selective bin copy — only entry points we actually invoke from runtime.
COPY --from=deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# -----------------------------------------------------------------------------
# Stage: full — final image with multimodal site-packages + mineru cache.
# -----------------------------------------------------------------------------
FROM runtime-base AS full
ARG PYTHON_VERSION
ENV TM_BUILD_FLAVOR=full
COPY --from=deps-full /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                      /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=deps-full /usr/local/bin/uvicorn /usr/local/bin/uvicorn
# mineru pre-warm cache (root-owned in builder; copy --chown to tm so the
# unprivileged runtime user can actually read it).
COPY --from=deps-full --chown=tm:tm /root/.cache/mineru /home/tm/.cache/mineru
