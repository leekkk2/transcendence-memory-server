# 健康检查与监控 / Health Check & Monitoring

## 快速健康检查

### 从服务端本机

```bash
curl -sS http://127.0.0.1:8711/health
```

### 从公网端点

```bash
curl -sS https://your-memory-endpoint.example.com/health
```

### 预期响应

```json
{
  "architecture": "lancedb-only",
  "auth_configured": true,
  "embedding_configured": true,
  "lancedb_available": true,
  "scripts_present": true,
  "runtime_ready": true,
  "available_containers": ["imac"],
  "warnings": []
}
```

## 完整验证流程

```bash
# 1. health
curl -sS -i http://127.0.0.1:8711/health

# 2. search
curl -sS -X POST http://127.0.0.1:8711/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","query":"test","topk":3}'

# 3. embed
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","background":true}'

# 4. typed ingest（按需）
curl -sS -X POST http://127.0.0.1:8711/ingest-memory/objects \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","objects":[]}'
```

## 服务状态检查

### Docker 部署

```bash
docker compose ps
docker compose logs backend --tail=100
```

### systemd 部署

```bash
systemctl status transcendence-memory-backend
journalctl -u transcendence-memory-backend -n 100 --no-pager
```

## 常见告警解读

| 症状 | 可能原因 |
|------|----------|
| `/health` 返回 `auth_configured: false` | `RAG_API_KEY` 未设置 |
| `/health` 返回 `embedding_configured: false` | `EMBEDDING_API_KEY` 未设置 |
| `/health` 返回 `lancedb_available: false` | LanceDB 依赖缺失或运行时目录不可用 |
| `/health` 不可达 | 服务未启动或端口被占用 |
| 公网 `/health` 5xx | 反向代理配置问题或后端服务不健康 |
