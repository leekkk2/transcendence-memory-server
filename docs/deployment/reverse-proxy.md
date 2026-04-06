# Reverse Proxy Configuration

## Overview

In production environments, it is recommended to expose the Memory API through a reverse proxy, providing HTTPS termination, domain binding, and access control.

## Nginx Configuration Reference

### Key Points

1. Proxy external HTTPS traffic to local `127.0.0.1:8711`
2. Configure SSL certificates (Let's Encrypt / certbot recommended)
3. Set appropriate timeouts (embed operations may take longer)
4. Forward the original Host header and client IP

### Minimal Nginx Configuration Template

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

        # Embed operations may take longer
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

### Verify the Reverse Proxy

```bash
# Access from external network
curl -sS https://your-memory-endpoint.example.com/health

# Verify the proxy chain
curl -sS -i https://your-memory-endpoint.example.com/search \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"test","query":"hello","topk":3}'
```

## Split-Machine Considerations

When exporting connection information in a split-machine topology, the `advertised_url` in `config.toml` must be set to the public domain/IP that the frontend can actually reach. Do not use `127.0.0.1` / `localhost` / private network addresses.

## Related Documentation

- [Quickstart](quickstart.md)
- [Environment Variable Reference](environment-reference.md)
