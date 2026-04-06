# 反向代理配置 / Reverse Proxy

## 概述

生产环境推荐通过反向代理暴露 Memory API，提供 HTTPS 终止、域名绑定和访问控制。

## Nginx 配置参考

### 核心要点

1. 将外部 HTTPS 流量代理到本地 `127.0.0.1:8711`
2. 配置 SSL 证书（推荐 Let's Encrypt / certbot）
3. 设置适当的超时（embed 操作可能耗时较长）
4. 传递原始 Host header 和客户端 IP

### 最小 Nginx 配置模板

```nginx
server {
    listen 443 ssl;
    server_name your-memory-endpoint.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://127.0.0.1:8711;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # embed 操作可能耗时较长
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

### 验证反向代理

```bash
# 从外部访问
curl -sS https://your-memory-endpoint.example.com/health

# 确认代理链路
curl -sS -i https://your-memory-endpoint.example.com/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"test","query":"hello","topk":3}'
```

## split-machine 注意事项

在 split-machine 拓扑下导出连接信息时，`config.toml` 的 `advertised_url` 必须设为前端实际可达的公网域名/IP，不能使用 `127.0.0.1` / `localhost` / 私有网段。

## 相关文档

- [快速入门](quickstart.md)
- [环境变量参考](environment-reference.md)
