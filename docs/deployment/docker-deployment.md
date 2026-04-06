# Docker 部署 / Docker Deployment

## 通用默认路径

Docker-first 是推荐的生产部署方式。

### 启动与检查

```bash
docker compose up -d
docker compose ps
docker compose logs rag-server --tail=100
```

### Lite / Full 构建规格

默认构建规格是 `lite`：

```bash
docker compose up -d --build
```

如果需要完整多模态依赖，显式切到 `full`：

```bash
BUILD_TARGET=full docker compose up -d --build
```

建议把 `BUILD_TARGET` 写入 `.env`，避免不同会话切换时出现构建口径漂移。

### 设备特定提醒

某些宿主机上，Docker 已安装但当前会话可能无法直接访问 Docker daemon，需要改走 sudo 路径：

```bash
sudo docker compose ps
sudo docker compose logs rag-server --tail=100
```

这属于**当前设备/当前权限模型下的现实差异**，不应被当成所有环境的通用默认。

### 环境变量注入

Docker 部署时，环境变量通过 `.env` 文件或 `docker-compose.yml` 的 `environment` 块注入。

完整变量列表见 [environment-reference.md](environment-reference.md)。

### 健康检查

```bash
curl -sS http://127.0.0.1:8711/health
```

重点关注：

- `build_flavor`
- `multimodal_capable`
- `degraded_reasons`

### 日志排查

```bash
docker compose logs rag-server --tail=200 --follow
```

## 相关文档

- [环境变量参考](environment-reference.md)
- [反向代理配置](reverse-proxy.md)
- [服务端排障](../operations/troubleshooting.md)
