# systemd 部署 / Systemd Deployment

## 概述

裸机 Linux 生产环境推荐使用 systemd 管理 Memory Server 进程。

## 创建 service 文件

```ini
# /etc/systemd/system/transcendence-memory-backend.service
[Unit]
Description=Transcendence Memory Backend Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/transcendence-memory-server
EnvironmentFile=/path/to/transcendence-memory-server/.env
ExecStart=/path/to/transcendence-memory-server/.venv-task-rag-server/bin/uvicorn \
    task_rag_server:app --app-dir scripts --host 0.0.0.0 --port 8711
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## 启用与管理

```bash
sudo systemctl daemon-reload
sudo systemctl enable transcendence-memory-backend
sudo systemctl start transcendence-memory-backend

# 查看状态
systemctl status transcendence-memory-backend

# 查看日志
journalctl -u transcendence-memory-backend -n 100 --no-pager
journalctl -u transcendence-memory-backend -f  # 实时跟踪
```

## .env 文件模板

```bash
WORKSPACE=/path/to/transcendence-memory-server
RAG_API_KEY=replace-me
EMBEDDING_API_KEY=replace-me
EMBEDDING_BASE_URL=https://your-embedding-endpoint/v1
EMBEDDINGS_BASE_URL=https://your-embedding-endpoint/v1
EMBEDDING_MODEL=gemini-embedding-001
```

完整变量列表见 [environment-reference.md](environment-reference.md)。

## 生产环境注意事项

- 确保 service 用户有权限读写 `WORKSPACE` 目录
- 若 venv 路径不同，调整 `ExecStart` 中的路径
- 推荐配合反向代理使用，详见 [reverse-proxy.md](reverse-proxy.md)

## 当前生产实例

当前线上 Eva 节点使用 `rag-everything.service` 作为 live unit name。
当文档化公共部署资产时，使用 `transcendence-memory-backend` 作为 wrapper / packaging name。
不要将这两个命名面合并。
