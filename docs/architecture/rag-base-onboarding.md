# Onboarding: Consuming the `rag-base` shared image

> **Audience**: external developers who want to build their own RAG / multimodal
> service on top of the same heavy base image this project publishes — or who
> simply want to understand how the multi-stage Docker build is layered.
>
> **TL;DR**: `rag-base` is a service-agnostic, publicly published base image
> (OS libs + Python multimodal deps + mineru model cache, **no application
> code**). Your service is `FROM ghcr.io/leekkk2/rag-base:<ver>` plus a thin
> code diff. Multiple full-flavor services then share the ~5 GB heavy layers on
> disk instead of each storing their own copy.

If you only want a running memory server, you don't need this doc — see the
[main README](../../README.md) Quick Start. This doc is for **building your own
image on top of the shared base**, or tuning the build for CPU-only / disk-
constrained hosts.

Related design docs:
[shared-image spec](./2026-06-01-rag-base-shared-image-spec.md) ·
[target Dockerfile + CI appendix](./2026-06-01-rag-base-target-dockerfile-and-ci.md).

---

## 1. What is `rag-base`?

A modern RAG service drags in a *lot* of heavy, slow-changing dependencies:
`torch`, `mineru` (PDF/image/table parsing), `opencv`, `transformers`, plus a
few hundred MB of pre-warmed mineru model cache. Together that's roughly **5 GB
of weight that almost never changes** — while the actual application code is a
few MB that changes every commit.

`rag-base` splits that reality into two parts:

| Concept | Contents | Changes |
|---------|----------|---------|
| **base** (`rag-base` / `rag-base-lite`) | OS libs + non-root user + Python deps (+ multimodal deps + mineru cache for the full variant). **No business code.** | Rarely (only when a dependency is bumped) |
| **service diff** | Your `scripts/` + `src/` + UI + app `ENV`/`EXPOSE`/`HEALTHCHECK`/`ENTRYPOINT` | Every commit |

So a service image is just `base + thin diff`:

```
rag-base  (~5 GB, shared, published, no code)
   └── + your code & app config  (a few MB)  =  your-service:full
```

**Why this matters — disk de-duplication across services.** Because every
full-flavor service is `FROM` the *same* `rag-base` layers, Docker stores those
~5 GB **once** and shares them across all consumers. Two services that each
parse PDFs locally no longer cost `2 × 5 GB` on the host — they cost
`5 GB + (a few MB) + (a few MB)`. The published base is purely a reuse / disk /
time optimization; see §4 for why it is **not** a build prerequisite.

Two published variants:

- **`ghcr.io/leekkk2/rag-base-lite`** — OS + base Python deps. No multimodal,
  no code. For text-only / vector-search services that never parse files
  locally.
- **`ghcr.io/leekkk2/rag-base`** — `rag-base-lite` + the multimodal increment
  (`torch` / `mineru` / `opencv` / ...) + mineru cache. The ~5 GB heavy base.

Both are published **public** on GHCR — anyone can `docker pull` them. They
contain only open-source dependencies; **no keys, no secrets, no app code**.

---

## 2. How to consume it

Point your service's Dockerfile at the published base and add only your own
thin layer on top. Pin an explicit version (see §5).

### Full (multimodal) service — copy-paste example

```dockerfile
# syntax=docker/dockerfile:1.7
# Your multimodal RAG service = rag-base + your thin code diff.
FROM ghcr.io/leekkk2/rag-base:1.3-py3.13

# rag-base already provides: OS libs, the non-root `tm` user (UID/GID 10001),
# /data, all Python deps incl. torch/mineru/opencv, and the mineru model cache
# at /home/tm/.cache/mineru  → you only add code + app config.
WORKDIR /app
ENV WORKSPACE=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/scripts:/app/src \
    PATH="/app/scripts:${PATH}"

# Thin code layer goes on top (most-changed → keep below layers cached).
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py

EXPOSE 8711
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=60s \
    CMD ["python3", "/app/scripts/healthcheck.py"]
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
```

> The `tm` non-root user, the `/data` directories, and the mineru cache live in
> the base — you inherit them for free. Just `COPY --chown=tm:tm` your code so
> the runtime user can read it.

### Lite (text-only) service

If your service never parses files locally, build on the lite base — it skips
the ~4 GB multimodal increment entirely:

```dockerfile
# syntax=docker/dockerfile:1.7
FROM ghcr.io/leekkk2/rag-base-lite:1.3-py3.13
WORKDIR /app
COPY --chown=tm:tm scripts/ ./scripts/
COPY --chown=tm:tm src/ ./src/
RUN chmod 755 /app/scripts/*.sh /app/scripts/*.py
EXPOSE 8711
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
```

Build & run as usual:

```bash
docker build -t my-rag-service:full .
docker run -d -p 8711:8711 --env-file .env my-rag-service:full
```

---

## 3. ★ Configurable `torch` variant (CPU vs GPU)

`torch` is the single biggest knob on image size. The default and the opt-in
CPU path are both first-class — pick based on your host.

### Default — regular resolution (GPU users, zero config)

If you build the base **without** overriding anything, `torch` is resolved the
normal way from PyPI. On Linux that means the **CUDA build** — so GPU hosts get
GPU acceleration out of the box, with no extra flags. This is the default
precisely so GPU users are never surprised:

```bash
# Default build — CUDA torch on Linux, GPU works out of the box.
docker build --target rag-base -t rag-base:gpu .
```

If you want a GPU build pinned to a specific CUDA wheel index, pass it
explicitly (e.g. cu124):

```bash
docker build --target rag-base \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu124 \
  -t rag-base:gpu-cu124 .
```

### CPU / disk-constrained — opt-in CPU wheel

On a host **without a GPU** (or where you want to save disk), the CUDA build is
pure waste: ~4 GB of `nvidia-*` / `triton` libraries that will never run. Opt in
to the CPU wheel — mineru's CPU mode keeps **full capability** (zero feature
loss), it's just slower:

```bash
# CPU-only build — drops ~4 GB of CUDA libraries.
docker build --target rag-base \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu \
  -t rag-base:cpu .
```

The same `--build-arg` flows through to a full-flavor service build:

```bash
# CPU service image, fully self-contained (no GHCR pull, see §4).
docker build --target tm-full \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu \
  -t my-rag-service:full-cpu .
```

> **What this project's CI publishes**: the GHCR `rag-base` is built with the
> **CPU** variant (`TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu`),
> because the production host has no GPU — CUDA torch there is `cuda=False` dead
> weight that just inflates the image. If you pull the published base and need
> GPU, build your own base with the default (or a `cu*` index) instead of
> pulling.

| Build arg | Default | Effect |
|-----------|---------|--------|
| `TORCH_INDEX_URL` | *(empty)* | Regular PyPI resolution → CUDA on Linux (GPU ready) |
| `TORCH_INDEX_URL=.../whl/cpu` | — | CPU-only torch, ~4 GB smaller, no GPU |
| `TORCH_INDEX_URL=.../whl/cu124` | — | Pin a specific CUDA wheel index |
| `TORCH_VERSION` | `2.12.0` | torch version (compatible with `mineru[core]>=3.0.9`) |

---

## 4. OSS self-containment — build everything from scratch

The published GHCR base is a **convenience**, not a dependency. Every stage —
`rag-sys-base → rag-base-lite → rag-base → tm-full` — lives inside this repo's
single `Dockerfile`. You can build a full service image **without pulling any
published image**, starting from plain `python:3.13-slim`:

```bash
git clone https://github.com/leekkk2/transcendence-memory-server.git
cd transcendence-memory-server

# Builds rag-sys-base → rag-base-lite → rag-base → tm-full entirely locally.
docker build --target tm-full -t tm:selfcontained-full .
```

This proves the whole chain is closed inside the Dockerfile. Open-source
completeness is zero-loss:

- **Want it self-contained?** `docker build --target tm-full` — builds the base
  locally, depends on nothing external.
- **Want it fast / disk-light?** `FROM ghcr.io/leekkk2/rag-base:<ver>` — reuse
  the published heavy base (§2).

To verify the `base + diff` layering is real (multimodal in its own incremental
layer, thin code on top):

```bash
docker build --target rag-base      -t rag-base:test .
docker build --target rag-base-lite -t rag-base-lite:test .

# The multimodal pip-install layer sits *on top of* the base-lite layers,
# so Docker de-dups the shared base across services.
docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' rag-base:test

# tm-full's top layers are just COPY scripts/ + src/ + ui — KB~MB, no GB-sized
# site-packages COPY.
docker history --format '{{.Size}}\t{{.CreatedBy}}' tm:selfcontained-full | head -n 8
```

---

## 5. Version pinning convention

Base and service versions are **decoupled** — they release on independent
cadences.

| Image | Tag form | Example | Source of truth |
|-------|----------|---------|-----------------|
| `ghcr.io/leekkk2/rag-base` | `<rag-anything-major>-py<pyver>` | `1.3-py3.13` | `RAG_BASE_VERSION` (hand-maintained in CI) |
| `ghcr.io/leekkk2/rag-base-lite` | same | `1.3-py3.13` | same cadence as `rag-base` |
| `transcendence-memory-server` (Docker Hub) | `<ver>-lite` / `<ver>-full` | `0.17.3-full` | service git tag (`v*.*.*`) |

Rules of thumb:

- **Always pin an explicit base tag** in your consuming Dockerfile —
  `FROM ghcr.io/leekkk2/rag-base:1.3-py3.13`, never `:latest`. Think of it like
  a Podfile / lockfile declaration: you depend on a *specific* base version.
- `1.3` tracks the `raganything` major version; `py3.13` tracks the base's
  Python version.
- When the base is bumped (e.g. `raganything 1.3 → 1.4`, or `py3.13 → 3.14`),
  each consuming service **rebuilds against the new tag explicitly**. There is
  no silent floating dependency.
- Record the pairing in your release notes (e.g. "service `vX.Y.Z` is built on
  `rag-base:1.3-py3.13`") so a rollback knows which base it used.

---

## 6. The 8 stages at a glance

The single `Dockerfile` defines 8 stages. Build any of them with
`--target <stage>`.

| # | Stage | Base | Adds | Publishable? |
|---|-------|------|------|--------------|
| 1 | `ui-builder` | `node:20-alpine` | Builds the admin React SPA (`/ui/dist`) | no (build-only) |
| 2 | `deps` | `python:3.13-slim` | Base Python deps from `pyproject.toml` + `constraints.txt` | no |
| 3 | `deps-full` | `deps` | `.[multimodal]` extras + pre-warmed mineru cache | no (only its mineru cache is reused) |
| 4 | `rag-sys-base` | `python:3.13-slim` | OS libs (libgl1/glib/gomp/poppler/libmagic/gosu) + non-root `tm` user + `/data`. **No code, no app config.** | — (parent of the publishable bases) |
| 5 | **`rag-base-lite`** | `rag-sys-base` | `deps` site-packages + uvicorn. No multimodal, no code. | **✅ GHCR public** |
| 6 | **`rag-base`** | `rag-base-lite` | `.[multimodal]` (incremental, method A) + mineru cache. **★ ~5 GB shared base.** | **✅ GHCR public** |
| 7 | `tm-lite` | `rag-base-lite` | tm-server code + UI + app `ENV`/`EXPOSE`/`HEALTHCHECK`/`ENTRYPOINT` | Docker Hub `:<ver>-lite` |
| 8 | `tm-full` | `rag-base` | tm-server code + UI + app config | Docker Hub `:<ver>-full` |

Key layering facts:

- Stages 4–6 are **service-agnostic** — that's what lets `tm-server`,
  `memory-app-server`, and any future RAG service `FROM` the same base.
- Stage 6 (`rag-base`) adds multimodal via **`RUN pip install ".[multimodal]"`
  on top of `rag-base-lite`** (not a wholesale `COPY` of site-packages). This
  keeps the increment in its **own layer** so Docker de-dups the shared
  base-lite blob across services — the heart of the `base + diff` design.
- App-specific config (port `8711`, healthcheck script, entrypoint,
  `PYTHONPATH`) lives **only** in the `tm-*` service stages, never in the base.

---

## See also

- [Build flavors & quick start](../../README.md) — running the server itself
- [shared-image spec](./2026-06-01-rag-base-shared-image-spec.md) — the design
  rationale and requirements
- [target Dockerfile + CI appendix](./2026-06-01-rag-base-target-dockerfile-and-ci.md)
  — full Dockerfile, GHCR publish workflow, size-assertion gate
</content>
</invoke>
