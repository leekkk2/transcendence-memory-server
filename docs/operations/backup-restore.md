# 数据备份与恢复 / Backup & Restore

## 需要备份的内容

```
$WORKSPACE/
├── tasks/rag/containers/*/     # LanceDB 索引数据
├── tasks/active/               # 活跃任务卡
├── tasks/archived/             # 归档任务卡
├── memory/                     # Markdown memory
├── memory_archive/             # 归档 memory
└── */memory_objects.jsonl      # typed client objects
```

## 备份策略

### 最小备份

```bash
tar -czf backup-$(date +%Y%m%d).tar.gz \
  tasks/ memory/ memory_archive/
```

### 只备份 typed objects

```bash
find $WORKSPACE -name "memory_objects.jsonl" -exec cp {} backup/ \;
```

## 恢复

1. 停止服务
2. 恢复文件到 `$WORKSPACE`
3. 重启服务
4. 执行 `/embed` 重建索引（如 LanceDB 数据损坏）

```bash
# 重建所有 container 的索引
curl -sS -X POST http://127.0.0.1:8711/embed \
  -H "X-API-KEY: $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"container":"imac","background":false,"wait":true}'
```

## 注意事项

- LanceDB 索引可以从源数据重建，因此 `tasks/` 和 `memory/` 是最核心的备份对象
- `memory_objects.jsonl` 是 typed objects 的持久存储，丢失后无法自动恢复
- 备份文件不应包含 API key 或其他 secret
