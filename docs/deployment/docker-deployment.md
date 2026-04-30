# Docker Deployment

Docker is the supported production deployment path. Builds happen in CI or on
a workstation; the production host only `docker pull`s pre-built images.

## Quick start (production host)

Assumes a fresh host with Docker, docker compose v2, and (optionally) rclone
already configured. The transcendence-memory-server repo is checked out at a
known path — pick one and stick with it.

```bash
cd /path/to/transcendence-memory-server

# 1. Configure secrets (only on the host, never in git)
cp .env.example .env
$EDITOR .env                                  # set RAG_API_KEY, EMBEDDING_API_KEY, etc.

# 2. Optional per-host tweaks (mem caps, etc.)
cp docker-compose.override.example.yml docker-compose.override.yml
$EDITOR docker-compose.override.yml           # only if you need to override defaults

# 3. Install systemd units (one-time)
sudo bash deploy/install.sh                   # add --no-rclone-sync if no rclone

# 4. Pull and start
TM_IMAGE=docker.io/leekkk2/transcendence-memory-server:0.6.0-lite \
  docker compose pull
sudo systemctl start rag-everything

# 5. Verify
curl -sS http://127.0.0.1:8711/health | jq .status
bash deploy/smoke-test.sh                     # end-to-end test (writes a smoke memory)
```

## Choosing a flavor

| Flavor | Image tag suffix | Use when |
|--------|------------------|----------|
| `lite` | `:0.6.0-lite`, `:lite`, `:latest` | LanceDB vector search only — covers /search, /embed, /ingest-memory |
| `full` | `:0.6.0-full`, `:full` | + LightRAG knowledge graph + multimodal (PDF/image) ingest via mineru |

Set `TM_IMAGE` in `.env` (or export in the shell) to lock the chosen flavor:

```bash
TM_IMAGE=docker.io/leekkk2/transcendence-memory-server:0.6.0-full
```

## Rclone integration (without the deadlock risk)

If your host has rclone-mounted archives that should feed into the RAG index,
the v0.6.0 design **never bind-mounts the FUSE path into the container**.
Instead, a host-side systemd timer (`rclone-sync.timer`) rsyncs from the FUSE
mount into a regular ext4 docker volume, which the container reads as a plain
read-only mount.

This is what prevents the 2026-04-30 deadlock: when rclone misbehaves, the
sync timer skips a tick and retries later, but the container keeps serving
requests because it's reading from a regular filesystem, not from FUSE.

| Component | Lives on | Triggered by |
|-----------|----------|--------------|
| `rclone-sync.service` | host systemd | `rclone-sync.timer` (every 15min) |
| Docker volume `rclone-archive` | host disk (managed by Docker) | populated by sync.service |
| Container mount `/mnt/rclone/example-archive:ro` | inside the container | docker compose |

The sync service has a 5-minute hard timeout and runs at IO-idle priority, so
it can't itself trigger the kind of host saturation we saw before.

To install:

```bash
sudo bash deploy/install.sh                   # installs both rag-everything and rclone-sync
```

To skip rclone-sync (host doesn't use rclone):

```bash
sudo bash deploy/install.sh --no-rclone-sync
```

## Memory backups

Always run a backup **before** any deploy that might touch volume layout:

```bash
sudo bash deploy/backup-memories.sh /var/backups
# → /var/backups/tm-data-<timestamp>.tar.gz + .sha256
```

Restore (manual, requires explicit confirmation):

```bash
sudo bash deploy/backup-memories.sh --restore /var/backups/tm-data-<ts>.tar.gz
```

The backup tool runs an alpine container that tars the `tm-data` volume at
the volume's actual mount point, so it captures the live LanceDB tables and
`memory_objects.jsonl` files exactly as the running server sees them.

## Standard deploy flow (after a new tag is published)

```bash
cd /path/to/transcendence-memory-server
git fetch origin && git checkout v<new-version>
sudo bash deploy/backup-memories.sh                          # backup first
sudo systemctl reload rag-everything                          # = pull + up -d
bash deploy/smoke-test.sh                                     # validate
```

`systemctl reload rag-everything` runs `docker compose pull` and then
`docker compose up -d`, which only re-creates the container if the image
digest changed. Because compose has `pull_policy: always`, a stale local
image cache won't keep the old container running.

## Build locally (development)

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
# Or full flavor:
BUILD_TARGET=full docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

The dev compose binds `./scripts:/app/scripts:ro` so code edits hit immediately
without a rebuild. **Never use the dev compose in production** — it relaxes the
read-only rootfs and bumps memory caps to dev-friendly levels.

## Why the rootfs is read-only

`docker-compose.yml` sets `read_only: true` and exposes only:

- `/data` — the persistent named volume (LanceDB, memory_objects.jsonl)
- `/tmp` — tmpfs (128 MB)
- `/home/tm/.cache` — tmpfs (256 MB)
- `/home/tm/.cache/mineru` — `mineru-models` named volume (full only)
- `/mnt/rclone/example-archive` — `rclone-archive` named volume (read-only)

If anything in the container tries to write outside these paths, it fails fast.
This catches accidental temp-file writes that would otherwise bloat the
container layer and make capacity-planning unpredictable.

## Image size & multi-arch

Both flavors are published as multi-arch manifests (`linux/amd64` and
`linux/arm64`). Pulling auto-selects the right arch.

| Flavor | Approx size (compressed) | Notes |
|--------|--------------------------|-------|
| lite   | ~250 MB | LanceDB, FastAPI, lightrag stub |
| full   | ~1.4 GB | + raganything, mineru, opencv-headless, pre-warmed mineru models |

## Verifying a deploy

After `systemctl start rag-everything`, the smoke script (deploy/smoke-test.sh)
exercises the full read/write path against a throwaway container name. It:

1. Hits `/health` and checks `accepting_ingest=true`
2. Hits `/admin/system-health` and checks `admit_ok=true`
3. Posts a uniquely-tagged smoke memory via `/ingest-memory/objects`
4. Confirms an embed job appeared in `/jobs`
5. Polls `/search` until the memory becomes searchable
6. Deletes the smoke container in cleanup

Total runtime is ~30 s on healthy hosts; up to 90 s during cold-start.

## Rolling back

```bash
git checkout v<previous-version>
TM_IMAGE=docker.io/leekkk2/transcendence-memory-server:<previous>-<flavor> \
  docker compose pull && \
  docker compose up -d --force-recreate
```

The `tm-data` volume is unaffected — your memories survive any image-version
roll-forward or roll-back. The `mineru-models` volume is additive; older tags
just don't use the newer files.

## Related documentation

- [Environment Variable Reference](environment-reference.md)
- [Reverse Proxy Configuration](reverse-proxy.md)
- [Architecture: Docker redesign v0.6](../architecture/docker-redesign-v0.6.md)
