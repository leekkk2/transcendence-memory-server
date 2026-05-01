# Docker Redesign — v0.6.0

> **Status**: implemented
> **Scope**: Dockerfile, docker-compose.{yml,prod,dev}, .dockerignore, entrypoint.sh, ci.yml, deploy/systemd unit, docs sync
> **Goal**: eliminate historical debt, single source of truth, security-hardened, reproducible builds, deploy-only-by-pull

---

## 1. Why redesign (failure-mode catalog)

A series of production incidents on hosts running pre-v0.6 deployments shared the same root pattern: **the container's defenses against host-level failures were insufficient**. Concrete failure modes observed in the wild:

| Failure mode | Symptom | Real cause |
|--------------|---------|------------|
| Remote build allowed | host OOM during `docker compose build full` on a memory-constrained VPS | remote build was permitted in the deploy path at all |
| FUSE deadlock | rclone FUSE hang → dozens of D-state procs → load > 100 | container bind-mounted rclone FUSE; when FUSE hangs, container reads pile up in D-state |
| IO saturation | docker compose pull stuck → cascading D-state across other host services | host IO queue saturated by container's foreground ingest |
| Stale service unit | `rag-everything.service` looped on 203/EXEC for hundreds of thousands of restarts | service file pointed at a script path that had been moved; nobody noticed |
| Memory drift | container memory limit `1500m` (host override) vs Dockerfile assumption `3g` | dev/prod inconsistency; container starves before host signals back-pressure |

The root problem is **drift across configuration surfaces**: Dockerfile, three compose files, an off-repo override on the host, a systemd unit pointing at the old native script, hard-coded version paths, and dual sources of truth between `pyproject.toml` and the Dockerfile. Each surface drifts on its own schedule and the failure modes compound.

A patch-style fix (the v0.5.10 line carries plenty of those) keeps the existing surfaces and adds new ones. This document records the holistic redesign that **collapses surfaces**, **enforces a single source of truth**, and **aligns the local repo with the production deploy path**.

---

## 2. Audit findings — full catalog

### 2.1 HIGH severity

1. **Container runs as root.** No `USER` directive in the Dockerfile. Any RCE in FastAPI = root inside the container. With a bind-mount of an external archive (e.g. `/mnt/archive:ro,slave`), root in the container can chmod-fight with the mount (limited blast radius because of `:ro`, but still wrong default).
2. **`opencv-python → opencv-python-headless` swap is fragile.** `pip uninstall + force-reinstall` in builder-full breaks if RAGAnything ever pins `opencv-python==X` exactly. Should use a pip constraints file (`PIP_CONSTRAINT`) so we never resolve `opencv-python` non-headless in the first place.
3. **Hard-coded `python3.13` site-packages path** in the multi-stage `COPY --from=builder` lines. A Python minor-version bump in the base image silently breaks the build. Use `${PY_SITE_PACKAGES}` ARG resolved by `python -c` at build time.
4. **No constraints / lockfile.** Each build resolves transitive deps independently. A breaking patch release in a dep can take down a CI build silently.
5. **`COPY --from=builder /usr/local/bin`** drags every console-script entry-point even though we only run `uvicorn`. Inflates image, increases attack surface (e.g., `mineru` CLI shipped to runtime).
6. **`.env.example` is the only doc on which keys matter.** The Dockerfile / entrypoint / arch_detect each maintain their own keylist. They drift.

### 2.2 MEDIUM severity

7. **Three sources of truth for "which flavor"**: `BUILD_TARGET=` (dev compose), `TM_IMAGE=` (main compose), `target:` (Dockerfile). User confusion.
8. **`docker-compose.dev.yml` allows `mem_limit: 6g`** with no comment justifying. Different from prod's 3g. Different from override's 1.5g. Three values, three files, no tests.
9. **`docker-compose.prod.yml` redefines `healthcheck`** identically to main compose. Pure duplication, drifts.
10. **Server-side override pins `mem_limit: 1500m`** and bind-mounts rclone — **this file is not in the repo**. Single biggest drift surface.
11. **Default port binding `0.0.0.0:8711`** in main compose. Override fixes it to 127.0.0.1, but if anyone runs `docker compose up` without prod overlay, they get an open API. Default should be safe.
12. **Inconsistent compose v2 syntax** — `cpus`, `mem_limit`, `oom_score_adj` at top-level mixed with healthcheck dict. Should pick deploy.resources or top-level keys consistently.
13. **`mineru[core]` model download happens at first request.** Several hundred MB pulled at runtime — first /documents/file call hangs for minutes. Pre-download in build stage.
14. **No mineru cache volume.** Even if pre-downloaded, `docker compose down -v` blows away the models. Need a separate named volume.
15. **systemd unit `rag-everything.service`** lives at `/etc/systemd/system/rag-everything.service` on the host but **isn't tracked in the repo**. The "fix" we just shipped lives only on the remote disk.
16. **Healthcheck installs curl** in runtime-base (lines in the apt install) inflating image. A 30-line Python script can do the same job using only stdlib.
17. **Dual deps**: `pip install fastapi uvicorn ...` in Dockerfile **and** `dependencies = [...]` in `pyproject.toml`. They overlap; resolution divergence is invisible.
18. **`python-multipart` declared twice** — once in main `dependencies`, again in `[multimodal]` extras.
19. **`docker-validate` job in CI builds for `linux/amd64` only**, but `publish-docker` builds `linux/amd64,linux/arm64`. So arm64 only gets validated at tag time. Pre-tag PRs miss arm64 regressions.
20. **CI `docker-validate` and `publish-docker` build the same image twice** — `docker-validate` for fast PR feedback, `publish-docker` for tag publish. We never reuse the layer cache between them.

### 2.3 LOW severity / tech debt

21. **`scripts/run_task_rag_server.sh`** — relict of native systemd path. Now obsolete. Move to `dev/legacy/`.
22. **`scripts/bootstrap_dev.sh`** — same. Move to `dev/legacy/`.
23. **`scripts/preflight_check.sh`** — useful only for native install. Make Docker-aware or move.
24. **`docs/deployment/systemd-deployment.md`** — documents the legacy native path. Replace with "systemd-managed docker compose" doc that matches what we actually use.
25. **`README.md` exception in `.dockerignore`** is fine, but undocumented.

### 2.4 External archive bind-mounts: feature, with FUSE caveats

A common deployment pattern bind-mounts an external archive (rclone, NFS, SSHFS, etc.) into the container so `scripts/sync_rclone_archive_to_memory_objects.py` can read from remote storage without copying data. Operators who use this pattern depend on it to feed pre-existing archive data into the RAG index.

But a direct FUSE bind-mount has consequences:
- Container reads to the archive path are subject to host FUSE health.
- If FUSE blocks, in-container processes block in D-state too.
- `:slave` propagation is correct (host→container only), but doesn't help with the latency.

**Mitigation in v0.6**: the queue-worker design (introduced in v0.5.10) addresses the synchronous-block problem — archive-touching ingest is enqueued, not blocking. v0.6 also ships a host-side `rclone-sync.timer` (see `deploy/systemd/rclone-sync.service`) that **rsyncs from FUSE into a regular ext4 docker volume** on a 15-minute cadence. The container then mounts that volume — never the FUSE path itself — so FUSE health can never block container reads in D-state.

For hosts that still want a direct FUSE bind-mount, declare `RequiresMountsFor=<your-mount>` via a systemd drop-in so the unit waits for FUSE to be live before starting the container.

---

## 3. Target architecture

### 3.1 File layout (after redesign)

```
transcendence-memory-server/
├── Dockerfile                          # single multi-stage; lite + full targets
├── docker-compose.yml                  # baseline; binds 127.0.0.1, expects pre-built image
├── docker-compose.override.example.yml # template for host-specific overrides (rclone, mem)
├── docker-compose.dev.yml              # dev: enables build + bind-mount source
├── .dockerignore                       # tightened
├── deploy/
│   ├── systemd/
│   │   └── rag-everything.service      # tracked in git, copied to /etc/systemd/system on install
│   └── install.sh                      # one-shot: copies systemd unit, daemon-reload, enable
├── dev/
│   └── legacy/                         # bootstrap_dev.sh, run_task_rag_server.sh moved here
├── scripts/                            # only files actually used at runtime
│   ├── entrypoint.sh                   # slimmer: no key validation (server does it via /health)
│   ├── healthcheck.py                  # NEW: stdlib-only HTTP healthcheck
│   └── ...                             # task_rag_*, server_protection, job_queue, job_worker
├── constraints.txt                     # NEW: pip constraints (locks transitive deps)
├── pyproject.toml                      # SINGLE source of truth for deps
└── docs/
    ├── architecture/
    │   └── docker-redesign-v0.6.md     # this file
    └── deployment/
        ├── docker-deployment.md        # rewritten to match new flow
        └── systemd-managed-compose.md  # NEW (replaces systemd-deployment.md)
```

### 3.2 New Dockerfile structure

```dockerfile
# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.13
ARG PYTHON_IMAGE=python:${PYTHON_VERSION}-slim-bookworm
ARG TM_VERSION=dev

# ================================================================
# Stage 1: deps — resolve once, cache aggressively
# ================================================================
FROM ${PYTHON_IMAGE} AS deps
ARG PIP_CONSTRAINT=/build/constraints.txt
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build

# Copy dependency manifests only — maximum layer cache hit
COPY pyproject.toml constraints.txt README.md ./
COPY src/tm_server/__init__.py ./src/tm_server/__init__.py

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt . \
    && python -c "import sys, json; print(json.dumps({'site': next(p for p in sys.path if 'site-packages' in p)}))" > /build/_pyinfo.json

# ================================================================
# Stage 2: deps-full — adds multimodal extras with constraints
# ================================================================
FROM deps AS deps-full
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --constraint constraints.txt ".[multimodal]"

# Pre-warm mineru models so first /documents/file isn't a 5-min download
RUN python -c "import mineru; mineru.cli.warmup_models()" || true

# ================================================================
# Stage 3: runtime-base — system deps + non-root user
# ================================================================
FROM ${PYTHON_IMAGE} AS runtime-base
ARG PYTHON_VERSION
ARG TM_VERSION
LABEL org.opencontainers.image.title="transcendence-memory-server" \
      org.opencontainers.image.version="${TM_VERSION}" \
      org.opencontainers.image.source="https://github.com/leekkk2/transcendence-memory-server"

# Minimal runtime libs — no curl (healthcheck uses stdlib python)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 poppler-utils libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — UID 10001 picked deliberately above default-system range
RUN groupadd --system --gid 10001 tm \
    && useradd --system --uid 10001 --gid tm --home /app --shell /usr/sbin/nologin tm

# Pre-create data dir with correct ownership; bind-mount overrides at runtime
RUN install -d -o tm -g tm /data /data/tasks /data/memory /data/memory_archive

WORKDIR /app
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py

ENV WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/scripts:${PATH}"

USER tm
EXPOSE 8711
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=20s \
    CMD ["python3", "/app/scripts/healthcheck.py"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]

# ================================================================
# Stage 4a: lite — only the deps stage's site-packages
# ================================================================
FROM runtime-base AS lite
ENV TM_BUILD_FLAVOR=lite
COPY --from=deps /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                 /usr/local/lib/python${PYTHON_VERSION}/site-packages
# Selective bin copy — only entry points we actually invoke
COPY --from=deps /usr/local/bin/uvicorn /usr/local/bin/uvicorn

# ================================================================
# Stage 4b: full — multimodal site-packages + mineru models
# ================================================================
FROM runtime-base AS full
ENV TM_BUILD_FLAVOR=full
COPY --from=deps-full /usr/local/lib/python${PYTHON_VERSION}/site-packages \
                      /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=deps-full /usr/local/bin/uvicorn /usr/local/bin/uvicorn
# Mineru's pre-warmed model cache (only present in full)
COPY --from=deps-full --chown=tm:tm /root/.cache/mineru /home/tm/.cache/mineru
```

Notable changes:
- **Single `deps` stage** for both flavors via `deps-full FROM deps` → maximum cache reuse
- **`PIP_CONSTRAINT`** instead of post-install force-reinstall — opencv-python-headless is pinned in `constraints.txt`, raganything won't override
- **`USER tm`** non-root user with stable UID
- **Selective bin copy** (`uvicorn` only, not all of `/usr/local/bin`)
- **Healthcheck** uses pure-Python script — no curl in image
- **OCI labels** for image provenance
- **Mineru pre-warm** in build stage so first multimodal request isn't slow
- **`${PYTHON_VERSION}`** ARG — bump bases without editing every COPY

### 3.3 New compose layout

```yaml
# docker-compose.yml — baseline, safe defaults
services:
  rag-server:
    image: ${TM_IMAGE:?set TM_IMAGE (e.g. docker.io/leekkk2/transcendence-memory-server:0.6.0-lite)}
    pull_policy: always              # ← new: ensures `up -d` after `pull` actually re-creates
    ports:
      - "127.0.0.1:${TM_PORT:-8711}:8711"   # ← localhost-only by default
    volumes:
      - tm-data:/data
      - mineru-models:/home/tm/.cache/mineru   # full flavor only; harmless for lite
    env_file:
      - path: .env
        required: false
    environment:
      WORKSPACE: /data
    restart: unless-stopped
    stop_grace_period: 30s
    mem_limit: ${TM_MEM_LIMIT:-3g}
    memswap_limit: ${TM_MEM_LIMIT:-3g}
    mem_reservation: ${TM_MEM_RESERVATION:-1g}
    cpus: ${TM_CPUS:-1.5}
    pids_limit: ${TM_PIDS_LIMIT:-256}
    oom_score_adj: ${TM_OOM_SCORE_ADJ:-500}
    read_only: true                  # ← new: rootfs is read-only (writes go to volumes/tmpfs)
    tmpfs:
      - /tmp:size=128m,mode=1777
      - /home/tm/.cache:size=256m   # ← runtime cache scratch
    security_opt:
      - no-new-privileges:true
    healthcheck:
      test: ["CMD", "python3", "/app/scripts/healthcheck.py"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging:
      driver: json-file
      options:
        max-size: "20m"
        max-file: "5"

volumes:
  tm-data:
  mineru-models:
```

```yaml
# docker-compose.override.example.yml — template, copy to .override.yml on each host
services:
  rag-server:
    # If this host mounts rclone (or any FUSE archive) and the RAG sync script
    # needs to read from it, expose that path read-only into the container.
    # `slave` propagation = host changes propagate to container, but not vice versa.
    # Replace <HOST_ARCHIVE_PATH> with your actual rclone/NFS/etc. mount.
    volumes:
      - <HOST_ARCHIVE_PATH>:/mnt/archive:ro,slave
    # Tighten memory if this host runs many other tenants
    mem_limit: 1500m
    memswap_limit: 1500m
```

```yaml
# docker-compose.dev.yml — dev only: build locally, bind-mount source for live edit
services:
  rag-server:
    image: transcendence-memory-server:dev-${BUILD_TARGET:-lite}
    build:
      context: .
      target: ${BUILD_TARGET:-lite}
      args:
        TM_VERSION: dev-${USER:-local}
    pull_policy: never
    volumes:
      - ./scripts:/app/scripts:ro      # live-edit server code
    mem_limit: 6g                      # generous for local dev
    memswap_limit: 6g
    read_only: false                   # tests may write to /tmp etc.
```

`docker-compose.prod.yml` is **deleted**. Its only contents (bind to 127.0.0.1, larger logs) move into the baseline `docker-compose.yml` since they're sane production defaults that don't break dev.

### 3.4 systemd unit (tracked)

```ini
# deploy/systemd/rag-everything.service
[Unit]
Description=transcendence-memory-server (docker compose stack)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target
# Optional: declare any FUSE/network mount the container reads from via drop-in:
#   /etc/systemd/system/rag-everything.service.d/rclone.conf
#     [Unit]
#     RequiresMountsFor=/mnt/your-rclone-mount

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/transcendence-memory-server   # adjust if cloned elsewhere
ExecStart=/usr/bin/docker compose pull
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
ExecReload=/usr/bin/docker compose pull && /usr/bin/docker compose up -d
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
```

Improvements vs ad-hoc unit files seen in the wild:
- **No hardcoded archive path.** Hosts that bind-mount FUSE-backed storage add a drop-in with `RequiresMountsFor=` so systemd waits for the mount before the container starts — no more "container starts, ingest tries to read FUSE, FUSE not ready, D-state".
- `pull` runs before `up -d` so `systemctl reload rag-everything` is the canonical "deploy latest tag" command.
- `TimeoutStartSec=300` for full-flavor first-pull (multimodal image is large).

### 3.5 New healthcheck script

```python
#!/usr/bin/env python3
"""Tiny stdlib-only liveness probe — replaces curl in the image.

Reads $TM_HEALTH_PORT (default 8711) and hits /health. Exit 0 on 200, 1 otherwise.
"""
import http.client, os, sys

port = int(os.environ.get("TM_HEALTH_PORT", "8711"))
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
try:
    conn.request("GET", "/health")
    resp = conn.getresponse()
    sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
finally:
    conn.close()
```

### 3.6 constraints.txt (pip lock)

```
# Pinned to keep multimodal dep tree stable & headless.
opencv-python-headless==4.10.0.84
opencv-contrib-python-headless==4.10.0.84
# Force opencv-python OUT of the resolution tree — raganything declares it loose
opencv-python==4.10.0.84  # but we never install this; it's here to pin if it sneaks in
numpy<2
pyarrow>=15
```

`pip install --constraint constraints.txt` ensures every transitive resolution honors these versions. Eliminates the post-install force-reinstall.

### 3.7 .dockerignore (tightened)

```
.git
.gitignore
.gitattributes
.venv
venv
env
*.egg-info
__pycache__
*.pyc
*.pyo
.pytest_cache
.mypy_cache
.ruff_cache
htmlcov
.coverage
coverage.xml
release-assets
.vscode
.idea
.DS_Store
*.log
data/
tmp/
*.lance
*.db
docs/
ops-notes/
*.md
!README.md
docker-compose*.yml
.dockerignore
.github/
.env
.env.*
!.env.example
dev/
deploy/
tests/
```

### 3.8 CI workflow changes

```yaml
docker-validate:
  needs: test
  runs-on: ubuntu-latest
  strategy:
    matrix:
      flavor: [lite, full]
      platform: [linux/amd64, linux/arm64]   # ← arm64 validation pre-tag
  ...

# Cache shared between docker-validate and publish-docker via GitHub registry cache backend
publish-docker:
  ...
  - uses: docker/build-push-action@v5
    with:
      cache-from: type=gha
      cache-to: type=gha,mode=max
```

---

## 4. Migration plan (one-shot, no patches)

Each step is a single commit, in order. No "fix later" placeholders.

| # | Action | Files touched |
|---|--------|---------------|
| 1 | Add `constraints.txt` + `scripts/healthcheck.py` + `deploy/systemd/rag-everything.service` + `dev/legacy/` move | new files; mv 2 scripts |
| 2 | Rewrite `Dockerfile` per §3.2 | Dockerfile |
| 3 | Rewrite `docker-compose.yml` per §3.3, **delete** `docker-compose.prod.yml`, rewrite `docker-compose.dev.yml` | compose files |
| 4 | Add `docker-compose.override.example.yml`; update `.gitignore` to exclude `.override.yml` (already excluded?) | new file, .gitignore |
| 5 | Tighten `.dockerignore` per §3.7 | .dockerignore |
| 6 | Sync CI workflow per §3.8 | .github/workflows/ci.yml |
| 7 | Slim entrypoint.sh — defer health/key validation to /health endpoint | scripts/entrypoint.sh |
| 8 | Sync docs: rewrite docker-deployment.md, replace systemd-deployment.md | docs/deployment/ |
| 9 | Bump `pyproject.toml` + `src/tm_server/__init__.py` to 0.6.0 | version |
| 10 | Local validation: `docker buildx build --target lite` for amd64 + arm64; smoke run | (build only) |
| 11 | Tag `v0.6.0`, push to GitHub. Wait for CI to publish images. | git tag |
| 12 | On the deploy host: run `sudo bash deploy/install.sh`, ensure `.env` and any host-specific `docker-compose.override.yml` are in place, then `systemctl reload rag-everything` (or let the auto-deploy workflow do it) | host |

---

## 5. Validation matrix

After each step (1–9 local, 10 build, 11 CI, 12 deploy):

| Check | Command | Expected |
|-------|---------|----------|
| Lite image size | `docker image inspect tm:0.6.0-lite --format '{{.Size}}'` | < 800 MB (current ~1.1 GB) |
| Full image size | same for `:0.6.0-full` | < 3.5 GB |
| Image runs as non-root | `docker run --rm tm:0.6.0-lite id` | uid=10001(tm) |
| Healthcheck works | `docker run -d --name x ... && docker inspect x --format '{{.State.Health.Status}}'` after 30s | `healthy` |
| /health returns 200 | `curl -sk http://127.0.0.1:8711/health` | JSON body w/ `status:ok` |
| Build cache reuse | second `docker buildx build` (no source change) | < 30 s |
| Local pytest | `pytest -q` | 77/77 passing |
| Production smoke | after deploy: `bash deploy/smoke-test.sh` + verify queue worker via `/admin/system-health` | smoke passes, `worker_running=true` |

---

## 6. Rollback plan

If anything breaks during deploy step 12:

```bash
# On the host (in your repo working directory, e.g. /opt/transcendence-memory-server)
git checkout v0.5.10                                   # previous tag
TM_IMAGE=docker.io/leekkk2/transcendence-memory-server:0.5.10-full \
  docker compose pull && docker compose up -d --force-recreate
```

The data volume `tm-data` is unaffected — the LanceDB tables persist across rollbacks.
The new `mineru-models` volume is additive; rolling back doesn't break it.

---

## 7. Out of scope for v0.6.0

Deliberately deferred to keep this delivery atomic:

- **Distroless final stage** (`gcr.io/distroless/python3-debian12`). Saves ~200 MB but breaks debug shells; consider for v0.7.
- **OCI image signing** with `cosign`. Worth doing once we have a Sigstore policy.
- **Per-tenant resource quotas** (multiple containers for prod isolation). Single-container is fine for current load.
- **Full SBOM generation** in CI. Can layer on later.

---

## 8. Decisions captured

The following design decisions are now part of v0.6 and surfaced here so future
contributors can challenge them with full context:

1. **Single-flavor default**: `lite` is default; `full` requires explicit opt-in via `TM_IMAGE` tag.
2. **127.0.0.1-only port binding by default** — anyone wanting remote access must override.
3. **`docker-compose.prod.yml` deleted** — the convention is "main compose is production-safe; `override.yml` customizes per host".
4. **`read_only: true` rootfs** — prevents accidental in-container writes outside volumes; tests adjusted accordingly.
5. **Tracked systemd unit at `deploy/systemd/`** — replaces ad-hoc unit files maintained per host.
6. **rclone archive bind-mount is opt-in** — baseline compose does not bind any external archive; operators that want it copy `docker-compose.override.example.yml` and add a `<HOST_ARCHIVE_PATH>:/mnt/archive:ro,slave` line.
