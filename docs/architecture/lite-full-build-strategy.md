# Lite / Full 构建策略设计文档

> 当前状态（2026-04-06）：Docker 多 target、`BUILD_TARGET=full` 切换、health flavor 字段、CI 双规格构建/发布已落地；Compose profile 方案未实现，当前以显式 `BUILD_TARGET` 作为唯一支持入口。

> 目标：在 **同一服务边界** 下，为 `transcendence-memory-server` 提供 `lite` / `full` 两种构建规格，并通过 Docker 多 target + Compose profile / 参数切换完成统一部署。

## 1. 设计目标

### 核心目标
- 保持 **同一服务名、同一 API 边界、同一仓库**
- 通过构建规格而不是分叉服务，实现轻量版与全量版共存
- 默认体验优先：开源用户可快速启动 lite
- 需要 multimodal / rag-everything 时再切 full

### 非目标
- 不拆成两个独立产品
- 不维护两套不同 API
- 不引入两份长期分叉的 Docker / Compose / Docs

---

## 2. 设计原则

1. **同一产品，不同规格**
   - lite / full 是 build flavor，不是两个后端项目

2. **默认轻量，按需增强**
   - `lite` 作为默认构建目标
   - `full` 作为高配构建目标

3. **构建裁剪优于运行时开关**
   - 不只是 `ENABLE_MULTIMODAL=true/false`
   - 而是在镜像构建层面控制是否安装重依赖

4. **运维心智统一**
   - 服务名保持一致
   - API 与 health 行为一致
   - 差异通过 architecture/modules 反映

---

## 3. 规格定义

## 3.1 lite 规格

### 包含能力
- FastAPI 服务
- LanceDB 检索
- Embedding ingest / search
- LightRAG 知识图谱查询
- structured ingest
- connection token 导出
- typed object ingest

### 不包含能力
- RAG-Anything
- MinerU 重型文档解析链路
- multimodal query / advanced document parsing

### 适用场景
- 默认开源部署
- CI / 快速 smoke test
- 资源受限主机
- 仅文本 / 结构化检索场景

---

## 3.2 full 规格

### 包含能力
- lite 全部能力
- RAG-Anything
- multimodal / rag-everything
- 文档多模态解析
- 图片 / 表格 / 公式等处理链路

### 适用场景
- 生产多模态场景
- 复杂 PDF / Office / 图片文档解析
- 需要完整 rag-everything 的部署

---

## 4. 目标架构

### 4.1 Dockerfile 多 target

建议 Dockerfile 至少包含：

- `base`：公共运行层
- `builder-lite`：安装项目本体 + 轻量依赖
- `builder-full`：在 lite 基础上增加 RAG-Anything / multimodal 依赖
- `lite`：最终轻量镜像
- `full`：最终全量镜像

### 4.2 Compose 切换方式

当前已实现的切换方式：

#### 方式 A：build target 参数
```bash
docker compose up -d --build
```
或
```bash
BUILD_TARGET=full docker compose up -d --build
```

当前约定：
- 默认：lite
- `BUILD_TARGET=full` 时：full
- Compose profile 未实现，不作为当前对外承诺

---

## 5. 配置行为约定

## 5.1 与环境变量的关系

环境变量仍然只表达“配置”，不直接决定“依赖是否存在”。

例如：
- `VLM_API_KEY` 代表用户想启用 multimodal
- 但如果当前构建规格是 lite，则 health 应明确提示：
  - `multimodal configured while running lite build`

### 原则
- **build flavor 决定依赖集合**
- **env 决定功能是否被启用/配置完成**

---

## 6. health 与可观测性设计

### 6.1 health 输出建议增强字段
建议新增：
- `build_flavor: lite | full`
- `multimodal_capable: true | false`
- `degraded_reasons: []`

### 6.2 行为规范
#### lite + 未配置 VLM
- 合法
- multimodal disabled

#### lite + 已配置 VLM
- 合法但降级
- 需要明确 warning

#### full + 已配置 VLM 且包可用
- multimodal ready

#### full + 已配置 VLM 但包缺失
- 视为构建异常
- 需要显式警告，不能静默成功

---

## 7. Docker / 依赖策略建议

## 7.1 lite 依赖策略
安装：
- 项目本体 `pip install .`
- FastAPI / uvicorn / lancedb / pyarrow / lightrag-hku 等基础依赖

### 目标
- 小镜像
- 快构建
- 稳定

## 7.2 full 依赖策略
在 lite 基础上追加：
- `raganything`
- 其所需 multimodal 依赖

### 推荐方向
优先使用官方公开安装方式，而不是硬编码私有 patch：

```bash
pip install raganything
# 或按需 extras
pip install 'raganything[all]'
```

若官方 GitHub 安装方式更稳定，也可：
```bash
pip install git+https://github.com/HKUDS/RAG-Anything.git
```

### 设计建议
后续应评估以下方案的稳定性与镜像体积：
1. `pip install raganything`
2. `pip install 'raganything[all]'`
3. `pip install git+https://github.com/HKUDS/RAG-Anything.git`

并在文档中明确：
- 默认 full 选用哪种
- 为什么选
- ARM / x86 差异

---

## 8. 文档与开源表达建议

### README 中建议新增
- Quick Start (lite)
- Full Multimodal Build
- 构建规格对比表

### 部署文档中建议新增
- 如何从 lite 升级到 full
- full 的镜像大小 / 构建耗时预期
- ARM 环境注意事项

### Troubleshooting 中建议新增
- `VLM_API_KEY` 已配置但 health 仍显示 multimodal disabled
- 如何判断当前是 lite 还是 full
- full 构建失败时如何排查 raganything 安装

---

## 9. 推荐实施步骤

### Phase 1：最小可行实现
- [x] Dockerfile 支持 lite/full target
- [x] docker-compose 支持 target 切换
- [x] health 增加 build flavor 信息

### Phase 2：文档完善
- [x] README / deployment / troubleshooting 补齐 lite/full 说明

### Phase 3：CI 与发布
- [x] CI 分别构建 lite/full
- [x] 发布镜像标签：
  - `:lite`
  - `:full`
  - `:latest` 默认指向 lite

---

## 10. 推荐结论

最佳实现不是多个长期分叉的后端容器，而是：

- **同一服务**
- **Docker 多 target（lite/full）**
- **Compose profile / 参数切换**
- **health 显式暴露当前规格与降级原因**

这样可以同时兼顾：
- 开源体验
- 生产可控性
- 文档清晰度
- 长期维护成本

---

## 11. Remaining Gaps

当前仍未落地：
1. Compose profile 方式的切换
2. 更完整的 Docker daemon 级 end-to-end 构建验收
3. 体积/耗时基准的正式记录
