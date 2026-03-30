# Transcendence Memory Server 演进整理说明

这个目录用于补录 **当前 `transcendence-memory-server` 在仓库之外真实发生过的演进过程**。

目标不是伪造原始历史，而是：

1. 尽量按阶段拆分提交
2. 让 `git log --oneline --decorate --graph` 一眼能看出版本演化
3. 保留每个阶段的背景、范围、兼容性与遗留问题

建议阅读顺序：

1. `README.md`
2. `phase-01-initial-eva-service-baseline.md`
3. `phase-02-lancedb-migration.md`
4. `phase-03-service-hardening-and-health.md`
5. `phase-04-skill-and-structured-ingest.md`
