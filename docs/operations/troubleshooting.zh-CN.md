# 服务端排障 / Server Troubleshooting

## 排查优先级

按以下顺序排查：

1. 服务/容器状态
2. 反向代理或 advertised URL 链路
3. API key / auth header 一致性
4. Provider 和运行时依赖
5. 后端日志中的 search/embed/ingest 错误
6. operator 文档是否与当前 backend runtime 真相一致

## 常见问题

### 5xx at public endpoint

通常是反向代理或后端健康问题：

```bash
# 检查后端服务状态
systemctl status transcendence-memory-backend
# 或 Docker
docker compose ps
docker compose logs rag-server --tail=100

# 检查 Nginx
nginx -t
journalctl -u nginx -n 50
```

### 401 / 403

API key 不匹配或 auth header 错误：

```bash
# 确认本机 RAG_API_KEY
echo $RAG_API_KEY

# 直接测试
curl -sS -i http://127.0.0.1:8711/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","query":"test","topk":3}'
```

### search 返回 HTTP 200 但 body 有错误

这不算成功，视为 rollout 失败。检查后端日志：

```bash
journalctl -u transcendence-memory-backend -n 200 --no-pager | grep -i error
```

### embed 失败

通常是 dependency / runtime / provider / persistence 问题：

```bash
# 确认 embedding 配置
echo $EMBEDDING_API_KEY
echo $EMBEDDING_BASE_URL

# 检查日志
docker compose logs rag-server --tail=200 | grep -i embed
```

### VLM 已配置但 multimodal 仍不可用

先看 `/health`：

```bash
curl -sS http://127.0.0.1:8711/health | python3 -m json.tool
```

重点判断：

- `build_flavor=lite`：说明你还在轻量构建，应重新用 `BUILD_TARGET=full docker compose up -d --build`
- `build_flavor=full` 但 `multimodal_capable=false`：说明 full 镜像里的多模态依赖没有准备好，需要重建 full 镜像并检查构建日志

### typed ingest 成功但 search 无结果

确认 embed/indexing 已完成，目标 container 已初始化：

```bash
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","background":false,"wait":true}'
```

### Docker daemon 不可访问

不要立刻写成"主机没有 Docker"。先检查当前上下文是否需要 sudo：

```bash
sudo docker compose ps
sudo docker compose logs rag-server --tail=100
```

这个判断是环境特定的，不应泛化成通用规则。

### full 构建失败

优先执行：

```bash
BUILD_TARGET=full docker compose build --no-cache
```

然后检查：

```bash
docker compose logs rag-server --tail=200
```

若 `/health` 仍显示 `full build missing raganything package` 或 `full build missing lightrag package`，说明镜像构建没有真正产出完整依赖集。

### ModuleNotFoundError

先完成项目级开发安装：

```bash
./scripts/bootstrap_dev.sh
source .venv-task-rag-server/bin/activate
```

## 命名边界提醒

- Eva 生产实例 live unit：`rag-everything.service`
- 公共部署资产 wrapper name：`transcendence-memory-backend`
- 不要将这两个命名面合并
