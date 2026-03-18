# RAG-everything (Eva)

统一部署在 Eva 的 RAG 服务，供 iMac/Eva/Aliyun 共用。Aliyun 不本地安装。

## 服务地址
- HTTPS: https://rag.zweiteng.tk
- 内部反代: 127.0.0.1:8711

## 鉴权
- Header: `X-API-KEY: <RAG_API_KEY>`
- 或 `Authorization: Bearer <RAG_API_KEY>`

RAG_API_KEY 存放于：`~/.openclaw/.env`

## 主要端点
- POST /search {container, query, topk}
- POST /build-manifest {container}
- POST /ingest-memory {container, memory_dir?, archive_dir?}
- POST /embed {container}

## Nginx 反代
- 配置文件：`docs/nginx-rag.zweiteng.tk.conf`

## 证书
- certbot 已申请并自动续期
- 证书路径：/etc/letsencrypt/live/rag.zweiteng.tk

## 启动与日志
- 启动脚本：`~/.openclaw/workspace/scripts/run_task_rag_server.sh`
- 日志：`~/.openclaw/workspace/logs/task_rag_server.log`

## 容器隔离
- tasks/rag/containers/{imac,eva,aliyun}

> 注意：不要提交密钥到仓库。
