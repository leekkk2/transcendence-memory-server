# rag-base 共享底座 — 发布与分发方案 (Release & Distribution)

> 配套 `rag-base-onboarding.md`(消费侧上手)。本文聚焦**发布侧 + 版本治理 + 依赖自动拉取模型**。
> 核心问题:rag-base 如何像 Podfile 那样被各服务声明依赖、自动拉取、共享复用。

---

## 1. 一句话模型:rag-base = 容器世界的 Podfile 依赖

`rag-base` 是把多服务共用的 ~2-5GB 多模态重依赖(mineru/torch/opencv/lightrag/lancedb + 系统库 + 非 root 用户 + mineru 模型缓存,**无业务代码**)抽出的**共享基础镜像**。各服务以 `FROM` 声明依赖,`docker build`/`pull` 自动拉取,Docker 内容寻址层保证本地只存一份。

| Podfile / CocoaPods 概念 | rag-base 对应 |
|---|---|
| `pod 'rag-base', '1.3-py3.13'` 声明 | 服务 Dockerfile `FROM ghcr.io/leekkk2/rag-base:1.3-py3.13` |
| `pod install` 自动拉取依赖 | `docker build` / `docker pull` 自动从 GHCR 拉 base(不在本地就拉) |
| 共享 pod 缓存(多 target 不重复) | Docker 内容寻址层:多服务 pin 同一 digest → base 本地**只存一份** |
| `Podfile.lock` 精确锁定 | `FROM ...@sha256:<digest>` 按 digest pin(完全可复现) |
| `pod update` | bump tag(`1.3`→`1.4`)后各服务重建 |

**结论:「依赖自动拉取、共享复用」已实现,无需额外工具。** 消费服务只写一行 `FROM`,其余由 Docker 完成。

---

## 2. 发布侧 (Publish) — 谁产出 rag-base、怎么推

### 2.1 发布载体:本仓库 CI `publish-rag-base` job → GHCR

- 触发:`v*` tag push(与服务发版同批,见 §4)。
- 产物:`ghcr.io/leekkk2/rag-base:<RAG_BASE_VERSION>` + `ghcr.io/leekkk2/rag-base-lite:<RAG_BASE_VERSION>`,多 arch(amd64+arm64)。
- 权限:job 级 `permissions: packages: write` + `GITHUB_TOKEN`,**无需额外 secret**。
- torch 变体:CI 构建发布的 base 用 **CPU wheel**(`--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu`),因部署主机(prod-host)无 GPU,剔除 ~4GB CUDA 库(见 DR-048 @ memory-app)。OSS 用户自建默认走常规(GPU 友好),详 onboarding §3。
- 门禁:`docker history` 体积断言(多模态 diff 预期 ~1.5GB,>3.5GB = CUDA 回归 → CI 红),保证去重收益不被 churn 打折。

### 2.2 首次发布一次性手动步骤

1. 推第一个含 `publish-rag-base` 的 `v*` tag。
2. 去 GHCR package 设置:把 `rag-base` / `rag-base-lite` 可见性设 **Public**(满足"谁都能 pull")。
3. 在 package settings 把本仓库加为 **linked repo**(让 `GITHUB_TOKEN` 后续持续有 push 权)。

### 2.3 不动现有服务镜像链路

`publish-rag-base` 是**新增独立 job**;现有 `publish-docker → Docker Hub`(`transcendence-memory-server:<ver>-{lite,full}`)链路**完全不变**。rag-base 只是额外发到 GHCR 供 `FROM` 复用。

---

## 3. 消费侧 (Consume) — 服务如何声明并自动拉取

### 3.1 声明依赖(= Podfile 行)

消费服务 Dockerfile 顶部:
```dockerfile
FROM ghcr.io/leekkk2/rag-base:1.3-py3.13 AS app
WORKDIR /app
COPY scripts/ src/ pyproject.toml README.md ./
RUN pip install --no-cache-dir .   # 只装 base 缺的本服务专属依赖 + 本包(src 可 import)
# app 级 ENV/EXPOSE/HEALTHCHECK/ENTRYPOINT ...
```
`docker build` 时若本地无该 base,Docker **自动从 GHCR 拉取**(= `pod install`)。memory-app-server 已采用此式。

### 3.2 自动去重(省盘的本质)

当 `tm-full` 与 `memory-app:full` 都 `FROM` 同一 `rag-base:1.3-py3.13`:
- 两者引用**同一 base layer digest** → 本地 overlay2 只存一份那 ~2GB(CPU 变体)/~5GB(GPU 变体)。
- 各自只新增**薄代码层**(MB 级)。
- 无 base 时 2 服务 ≈ 2×镜像全量;有共享 base ≈ 1×base + 2×薄 diff。**prod-host 实测净省 ~4-5GB**。

验证(prod-host):
```bash
docker system df -v | grep -E "rag-base|memory-app|transcendence"   # base 层 Shared，不翻倍
docker image inspect memory-app-server:full --format '{{json .RootFS.Layers}}'  # 前 N 层 digest 与 rag-base 一致
```

---

## 4. 版本治理 (Version Governance)

### 4.1 base 与服务版本解耦

| 镜像 | tag | 来源 |
|---|---|---|
| `rag-base{,-lite}` | `1.3-py3.13`(`<raganything 主版本>-py<pyver>`) | CI `RAG_BASE_VERSION`(手动维护) |
| `transcendence-memory-server`(Docker Hub) | `<ver>-{lite,full}` | `${GITHUB_REF_NAME#v}` |

base 改动频率远低于服务 → 独立打 tag。base 升级(raganything 1.3→1.4 或 py3.13→3.14)时才 bump `RAG_BASE_VERSION`。

### 4.2 两档 pin 强度(按需选)

- **tag pin(默认)**:`FROM rag-base:1.3-py3.13`。可读、够用;但 tag 可被重推(同 tag 内容可变)。
- **digest pin(= Podfile.lock,强复现)**:`FROM rag-base:1.3-py3.13@sha256:<digest>`。锁死内容,base 重推不影响已 pin 的服务。生产/CI 推荐。digest 获取:
  ```bash
  docker buildx imagetools inspect ghcr.io/leekkk2/rag-base:1.3-py3.13 --format '{{.Manifest.Digest}}'
  ```

### 4.3 升级流程(= pod update)

1. 在本仓库 bump `RAG_BASE_VERSION`(如 `1.4-py3.13`),发新 `v*` tag → CI 发布新 base。
2. 各消费服务改自己 Dockerfile 的 `FROM` 到新 tag/digest,CI 重建。
3. 在 release notes 关联记录"本次 `transcendence-memory-server:<ver>` 基于 `rag-base:<base-ver>`",便于回溯。

### 4.4 可选:集中版本清单(单一来源)

若多服务共享,可在组织层维护一份 `rag-base.lock`(或 shared `.env`)记录当前认可的 `rag-base` digest,CI/Dockerfile 统一读取,等价 Podfile.lock 的单一来源。当前 2 服务(tm/memory-app)规模下,各自 Dockerfile pin + release notes 关联已足够;服务数增长再引入集中清单。

---

## 5. OSS 自包含 fallback(不依赖已发布 base)

tm-server Dockerfile **内含全部 8 阶段**,`docker build --target tm-full` 可在本仓库内**完全自建** rag-base→tm-full,不拉任何 GHCR 镜像。即:
- 发布的 GHCR rag-base = **省盘/省时的便利层**(消费侧 `FROM` 直接拉)。
- 自包含 fallback = OSS 用户 clone 仓库即可自建,不被 GHCR 可用性绑架。

两条路并存:消费侧默认 `FROM` 拉取(Podfile 式),OSS/离线/审计场景走自建。

---

## 6. 发布前验收门禁(R10)

打 `v*` tag(触发发布)前,必须本机 container-equivalent smoke 绿:
```bash
# 自包含自建(CPU 变体,与发布一致)
docker build --target tm-full --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu -t tm:accept .
# 体积 + torch 变体核对(应 cuda=False、image 显著小于 CUDA 版)
docker run --rm tm:accept python -c "import torch,mineru; print(torch.__version__, torch.cuda.is_available())"
# /health full + 一次 PDF 走 mineru(见 onboarding §4 / target-dockerfile-and-ci §3)
```
绿了再 tag。详见 `RELEASE-VERIFICATION-GATE`(R10)。

---

## 7. 关联

- 消费侧上手:`rag-base-onboarding.md`
- 目标 Dockerfile + CI:`2026-06-01-rag-base-target-dockerfile-and-ci.md`
- 主 spec:`2026-06-01-rag-base-shared-image-spec.md`
- CPU-torch 决策(消费侧):memory-app `docs/decisions/DR-048-*.md`
