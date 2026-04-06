# Server Troubleshooting

## Triage Priority

Investigate in the following order:

1. Service / container status
2. Reverse proxy or advertised URL connectivity
3. API key / auth header consistency
4. Provider and runtime dependencies
5. search/embed/ingest errors in backend logs
6. Whether operator documentation matches the actual backend runtime state

## Common Issues

### 5xx at public endpoint

Usually a reverse proxy or backend health issue:

```bash
# Check backend service status
systemctl status transcendence-memory-backend
# Or Docker
docker compose ps
docker compose logs rag-server --tail=100

# Check Nginx
nginx -t
journalctl -u nginx -n 50
```

### 401 / 403

API key mismatch or incorrect auth header:

```bash
# Verify the local RAG_API_KEY
echo $RAG_API_KEY

# Direct test
curl -sS -i http://127.0.0.1:8711/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","query":"test","topk":3}'
```

### search returns HTTP 200 but body contains errors

This is not a success — treat it as a rollout failure. Check backend logs:

```bash
journalctl -u transcendence-memory-backend -n 200 --no-pager | grep -i error
```

### embed failure

Usually a dependency / runtime / provider / persistence issue:

```bash
# Verify embedding configuration
echo $EMBEDDING_API_KEY
echo $EMBEDDING_BASE_URL

# Check logs
docker compose logs rag-server --tail=200 | grep -i embed
```

### VLM configured but multimodal still unavailable

Check `/health` first:

```bash
curl -sS http://127.0.0.1:8711/health | python3 -m json.tool
```

Key indicators:

- `build_flavor=lite`: You are still on the lite build. Rebuild with `BUILD_TARGET=full docker compose up -d --build`
- `build_flavor=full` but `multimodal_capable=false`: The multimodal dependencies in the full image are not ready. Rebuild the full image and inspect the build logs

### typed ingest succeeds but search returns no results

Confirm that embed/indexing has completed and the target container is initialized:

```bash
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","background":false,"wait":true}'
```

### Docker daemon inaccessible

Do not immediately conclude that the host lacks Docker. First check whether the current context requires sudo:

```bash
sudo docker compose ps
sudo docker compose logs rag-server --tail=100
```

This determination is environment-specific and should not be generalized into a universal rule.

### full build failure

Run this first:

```bash
BUILD_TARGET=full docker compose build --no-cache
```

Then inspect:

```bash
docker compose logs rag-server --tail=200
```

If `/health` still reports `full build missing raganything package` or `full build missing lightrag package`, the image build did not produce the complete dependency set.

### ModuleNotFoundError

Complete the project-level dev install first:

```bash
./scripts/bootstrap_dev.sh
source .venv-task-rag-server/bin/activate
```

## Naming Boundary Reminder

- Eva production instance live unit: `rag-everything.service`
- Public deployment asset wrapper name: `transcendence-memory-backend`
- Do not merge these two naming surfaces
