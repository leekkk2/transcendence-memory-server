# Docker Deployment

## Default Path

Docker-first is the recommended production deployment method.

### Start and Verify

```bash
docker compose up -d
docker compose ps
docker compose logs rag-server --tail=100
```

### Lite / Full Build Targets

The default build target is `lite`:

```bash
docker compose up -d --build
```

To include full multimodal dependencies, explicitly switch to `full`:

```bash
BUILD_TARGET=full docker compose up -d --build
```

It is recommended to set `BUILD_TARGET` in `.env` to prevent build target drift across different sessions.

### Platform Support

- Published Docker images: `linux/amd64`, `linux/arm64`
- Linux hosts: run natively with Docker Engine
- macOS hosts: run Linux containers via Docker Desktop
- Windows hosts: run Linux containers via Docker Desktop / WSL2
- No native macOS container images are published
- No native Windows container images are published

If you are using an Apple Silicon Mac, pull the `linux/arm64` multi-arch image directly. Intel Macs and standard x64 Windows hosts will typically auto-match `linux/amd64`.

### Device-Specific Notes

On some hosts, Docker may be installed but the current session may not have direct access to the Docker daemon, requiring the sudo path:

```bash
sudo docker compose ps
sudo docker compose logs rag-server --tail=100
```

This is a **practical difference specific to the current device/permission model** and should not be treated as the universal default for all environments.

### Environment Variable Injection

For Docker deployments, environment variables are injected via the `.env` file or the `environment` block in `docker-compose.yml`.

See [environment-reference.md](environment-reference.md) for the full variable list.

### Health Check

```bash
curl -sS http://127.0.0.1:8711/health
```

Key fields to check:

- `build_flavor`
- `multimodal_capable`
- `degraded_reasons`

### Log Inspection

```bash
docker compose logs rag-server --tail=200 --follow
```

## Related Documentation

- [Environment Variable Reference](environment-reference.md)
- [Reverse Proxy Configuration](reverse-proxy.md)
- [Server Troubleshooting](../operations/troubleshooting.md)
