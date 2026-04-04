# 升级与迁移 / Upgrade & Migration

## 版本升级流程

1. **备份当前数据**：参见 [backup-restore.md](backup-restore.md)
2. **拉取最新代码**
3. **重新 bootstrap**：
   ```bash
   ./scripts/bootstrap_dev.sh
   ```
4. **检查环境变量变更**：对比 [environment-reference.md](../deployment/environment-reference.md)
5. **重启服务**
6. **验证健康**：参见 [health-check.md](health-check.md)

## 架构迁移历史

当前主链为 **LanceDB-only**。历史演进记录见 `docs/evolution/`。

### 已退出主链的功能

- `POST /build-manifest`：已 deprecated，仅保留 no-op 响应
- FAISS 相关路径：已在 LanceDB-only 迁移中移除

## 数据迁移

如果从旧版本迁移，可能需要重建 LanceDB 索引：

```bash
# 为每个 container 重建索引
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","background":false,"wait":true}'
```

## 注意事项

- 目录名与脚本名保留 `task_rag_*` 历史语义，当前不做重命名
- 升级后先验证 `/health` 再验证业务端点
