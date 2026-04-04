# Both Identity（Backend 阶段）/ 双角色身份 — 服务端部分

## 你是谁

你是 `both` 机器，当前处于 **backend 阶段**。

## 推荐顺序

1. 先完成 backend 部署与健康检查
2. 确认服务端所有端点可用
3. 导出连接信息给 frontend 阶段使用
4. 再切换到 frontend 阶段（参见 transcendence-memory skill 的客户端文档）

## 关键规则

- 不要跳过 backend 直接做 frontend 验证
- 若 `health` 没过，不要继续把问题当作前端调用问题

## 优先文档

1. `docs/deployment/quickstart.md`
2. `docs/operations/health-check.md`
3. `docs/operations/troubleshooting.md`
