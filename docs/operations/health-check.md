# Health Check & Monitoring

## Quick Health Check

### From the server host

```bash
curl -sS http://127.0.0.1:8711/health
```

### From the public endpoint

```bash
curl -sS https://your-memory-endpoint.example.com/health
```

### Expected response

```json
{
  "build_flavor": "lite",
  "multimodal_capable": false,
  "degraded_reasons": [],
  "architecture": "lancedb-only",
  "auth_configured": true,
  "embedding_configured": true,
  "lancedb_available": true,
  "scripts_present": true,
  "runtime_ready": {
    "search": true,
    "embed": true,
    "ingest_memory": true,
    "ingest_objects": true,
    "ingest_structured": true,
    "query": false,
    "documents_text": false
  },
  "available_containers": ["home"],
  "warnings": []
}
```

Field descriptions:

- `build_flavor`: Current image variant, `lite` or `full`
- `multimodal_capable`: Whether the current build actually has multimodal dependencies available
- `degraded_reasons`: List of degradation reasons for the current build/config combination

## Full Verification Flow

```bash
# 1. health
curl -sS -i http://127.0.0.1:8711/health

# 2. search
curl -sS -X POST http://127.0.0.1:8711/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"home","query":"test","topk":3}'

# 3. embed
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"home","background":true}'

# 4. typed ingest (as needed)
curl -sS -X POST http://127.0.0.1:8711/ingest-memory/objects \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"home","objects":[]}'
```

## Service Status Check

### Docker deployment

```bash
docker compose ps
docker compose logs rag-server --tail=100
```

### systemd deployment

```bash
systemctl status transcendence-memory-backend
journalctl -u transcendence-memory-backend -n 100 --no-pager
```

## Common Alert Reference

| Symptom | Possible Cause |
|---------|----------------|
| `/health` returns `auth_configured: false` | `RAG_API_KEY` not set |
| `/health` returns `embedding_configured: false` | `EMBEDDING_API_KEY` not set |
| `/health` returns `lancedb_available: false` | LanceDB dependency missing or runtime directory unavailable |
| `/health` returns `build_flavor: lite` with `degraded_reasons` mentioning lite build | VLM configured but image is still lite |
| `/health` returns `build_flavor: full` with `multimodal_capable: false` | Full build missing `raganything` / `lightrag` dependencies |
| `/health` unreachable | Service not running or port occupied |
| Public `/health` returns 5xx | Reverse proxy misconfiguration or unhealthy backend |
