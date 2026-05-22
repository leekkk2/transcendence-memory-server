#!/usr/bin/env python3
"""Worker runtime — per-container subprocess 的 embedding 调用入口。

v0.7.0 multi-embedding 升级（2026-05-16）：
  - 删除 module-level API_KEY / MODEL / EMBEDDINGS_BASE_URL 等全局常量。
  - 改为按 worker 启动时的 CONTAINER env 通过 EmbeddingRegistry 解析对应
    profile（per-container 进程 = per-route），调用 OpenAI-style /embeddings。
  - 保留 Google `AIza*` 原生 endpoint fallback 作为现有 special path（当
    profile.api_key 以 AIza 开头时走 generativelanguage.googleapis.com）。
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import requests


# 保证 worker subprocess（python /path/to/task_rag_runtime.py）能 import 同目录的
# embedding_registry / profiles_loader — 即使没作为 package 被 import。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# 双重 import + 模块身份归一。worker subprocess（bare import）与 server/package
# （scripts. 前缀）两种入口可能各加载一份 embedding_errors 副本 —— typed
# exception 的 isinstance 跨副本会失效。这里复用任一已加载副本，并登记到两个
# 模块名下，保证全进程只有一份类对象。
embedding_errors = (
    sys.modules.get('scripts.embedding_errors')
    or sys.modules.get('embedding_errors')
)
if embedding_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import embedding_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import embedding_errors  # type: ignore[no-redef]
sys.modules.setdefault('embedding_errors', embedding_errors)
sys.modules.setdefault('scripts.embedding_errors', embedding_errors)

# 通用 fallback 核心 —— worker subprocess 同步链路用 run_with_fallback_sync。
# 同款双重 import + 模块身份归一：worker（bare import）与 server（package
# import）必须共用同一份 breaker dict，否则跨副本计数失效。
model_fallback = (
    sys.modules.get('scripts.model_fallback')
    or sys.modules.get('model_fallback')
)
if model_fallback is None:  # pragma: no cover - 取决于运行入口
    try:
        import model_fallback  # type: ignore[no-redef]
    except ImportError:
        from scripts import model_fallback  # type: ignore[no-redef]
sys.modules.setdefault('model_fallback', model_fallback)
sys.modules.setdefault('scripts.model_fallback', model_fallback)


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'

# 重试配置：上游限速时（429/5xx）走指数退避 + 抖动。
# 单条 chunk 最坏耗时 = MAX_RETRIES * (TIMEOUT + 平均退避)，控制在 ~3 分钟内，
# 避免 ingest 卡住时主线程持有 materialized_rows 列表导致内存压力持续累积。
# 历史教训（2026-04-29）：MAX_RETRIES=6 + MAX_DELAY=60 让 ingest 单条最坏卡 ~9 分钟，
# 容器整体陷入 churn 推升 swap thrashing。
_EMBED_MAX_RETRIES = int(os.getenv('EMBEDDING_MAX_RETRIES', '3'))
_EMBED_RETRY_BASE_DELAY = float(os.getenv('EMBEDDING_RETRY_BASE_DELAY', '1.5'))
_EMBED_RETRY_MAX_DELAY = float(os.getenv('EMBEDDING_RETRY_MAX_DELAY', '30'))
_EMBED_TIMEOUT = float(os.getenv('EMBEDDING_TIMEOUT', '60'))

logger = logging.getLogger(__name__)
if not logger.handlers and not logging.getLogger().handlers:
    # 让脚本独立运行时也能看到重试日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=sys.stderr)


def container_dir(container: str) -> Path:
    base = TASKS / 'rag' / 'containers' / container
    base.mkdir(parents=True, exist_ok=True)
    return base


def lancedb_dir(container: str) -> Path:
    base = container_dir(container) / 'lancedb'
    base.mkdir(parents=True, exist_ok=True)
    return base


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        # RFC7231 HTTP-date 格式
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        delta = dt.timestamp() - time.time()
        return max(0.0, delta)
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    base = _EMBED_RETRY_BASE_DELAY * (2 ** attempt)
    base = min(base, _EMBED_RETRY_MAX_DELAY)
    # ±25% 抖动，避免多 worker 同时重试踩同一个限速窗口
    jitter = base * random.uniform(-0.25, 0.25)
    delay = max(0.5, base + jitter)
    if retry_after is not None:
        # 上游建议的等待时间为下界，但仍夹在 max_delay 内避免无限等
        delay = max(delay, min(retry_after, _EMBED_RETRY_MAX_DELAY))
    return delay


def _resolve_chain_for_worker() -> list:
    """根据 CONTAINER / TM_EMBEDDING_PROFILE_OVERRIDE env 解析 embedding profile 链。

    返回 ``[primary, *fallbacks]``，供 run_with_fallback_sync 按序尝试：
      1. TM_EMBEDDING_PROFILE_OVERRIDE — 显式单 profile override。override 是
         「就用这一条」语义，不展开 route 的 fallback，链仅一元素。
      2. CONTAINER / TM_WORKER_CONTAINER env → registry.resolve(container) 拿
         Route，展开 ``[embedding, *embedding_fallbacks]``。
      3. 缺失 → default route 展开（向后兼容 legacy env-only 部署 / 旧测试 /
         不指定 CONTAINER 的旧 docker-compose）。
    """
    try:
        from embedding_registry import get_registry  # type: ignore
    except Exception:  # pragma: no cover
        try:
            from scripts.embedding_registry import get_registry  # type: ignore
        except Exception:
            from .embedding_registry import get_registry  # type: ignore

    registry = get_registry()

    override = os.environ.get('TM_EMBEDDING_PROFILE_OVERRIDE', '').strip()
    if override:
        return [registry.get_profile(override)]

    container = os.environ.get('CONTAINER') or os.environ.get('TM_WORKER_CONTAINER')
    route = (
        registry.resolve(container) if container
        else registry._profiles.default_route
    )
    return [
        registry.get_profile(route.embedding),
        *(registry.get_profile(fb) for fb in route.embedding_fallbacks),
    ]


def _embed_text_single(
    profile, text: str, mode: str, title: str | None,
) -> np.ndarray:
    """单 profile 的同步 embedding 执行器 —— run_with_fallback_sync 的 per-profile 回调。

    profile 内部 429/5xx 走指数退避重试；4xx 非 429 立即抛 requests.HTTPError
    （上层判 non-fallback、直接上抛）；重试耗尽归一为 typed embedding_errors，
    让 fallback runner 按 error_class 决定是否切下一条 profile。

    provider == 'gemini_native' 时单 profile 走 Gemini 原生 `:embedContent`
    协议（单 text part），与多模态摄取同一向量空间。
    """
    if getattr(profile, 'provider', '') == 'gemini_native':
        try:
            from gemini_native_embed import embed_parts_sync, text_part  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - package import path
            from scripts.gemini_native_embed import (  # type: ignore[import-not-found]
                embed_parts_sync,
                text_part,
            )
        return embed_parts_sync(profile, [text_part(text)], mode=mode, title=title)

    api_key = profile.api_key
    if not api_key:
        raise RuntimeError(
            f"profile {profile.name!r} has no api_key (check api_key_env)"
        )

    url = f'{profile.base_url.rstrip("/")}/embeddings'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload: dict = {'model': profile.model, 'input': text}
    # Matryoshka：profile.request_dim 指定时传 OpenAI `dimensions=N`
    request_dim = getattr(profile, 'request_dim', None)
    if request_dim:
        payload['dimensions'] = int(request_dim)

    last_err: Exception | None = None
    # 重试耗尽后用这三项把失败归类成 typed exception（embedding_errors）。
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
    for attempt in range(_EMBED_MAX_RETRIES):
        retry_after: float | None = None
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=_EMBED_TIMEOUT)
            status = response.status_code
            if status == 429 or status >= 500:
                retry_after = _parse_retry_after(response.headers.get('Retry-After'))
                # 不读 body 拿到更多细节，但避免抛 raise_for_status 的细节膨胀
                snippet = response.text[:200] if response.text else ''
                last_err = RuntimeError(f'embedding upstream {status}: {snippet!r}')
                last_status = status
                last_retry_after = retry_after
                last_error_class = None
            elif status >= 400:
                # 4xx（鉴权 / 参数错误）不可重试，立即抛出
                response.raise_for_status()
            else:
                data = response.json()
                return np.array(data['data'][0]['embedding'], dtype='float32')
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
            last_status = None
            last_retry_after = None
            # 超时单列 timeout 类别；连接抖动归默认 transient
            last_error_class = (
                embedding_errors.TIMEOUT
                if isinstance(exc, requests.Timeout) else None
            )
        except requests.HTTPError:
            # 已被分类（4xx 非 429），直接抛
            raise
        except Exception as exc:  # JSON 解析失败、空 data 等也算可重试
            last_err = exc
            last_status = None
            last_retry_after = None
            last_error_class = None

        if attempt == _EMBED_MAX_RETRIES - 1:
            break
        delay = _backoff_delay(attempt, retry_after)
        logger.warning(
            'embed_text attempt %d/%d failed: %s; retrying in %.1fs',
            attempt + 1, _EMBED_MAX_RETRIES, last_err, delay,
        )
        time.sleep(delay)

    # 重试耗尽 —— 归一为 typed exception，让上层 fallback / backlog 调度按
    # error_class 决策（quota/timeout/transient 可切下一条 profile）。
    raise embedding_errors.make_embedding_error(
        f'Embedding request failed after {_EMBED_MAX_RETRIES} retries: {last_err}',
        status=last_status,
        retry_after=last_retry_after,
        error_class=last_error_class,
    )


def embed_text(
    text: str, mode: str | None = None, title: str | None = None,
) -> np.ndarray:
    """单条文本 embedding 调用，专供 worker 使用。

    路由：CONTAINER env → registry.resolve → ``[primary, *fallbacks]`` 链。
    主 profile 不可用（429/5xx/超时/typed quota 等）时由 run_with_fallback_sync
    按序切下一条备用 profile，并与 server 端共用同一份 circuit breaker
    （category=embed）；4xx 非 429 / permanent 错误不前进、原样上抛。

    历史的「`AIza` 开头 key → Google native endpoint」单点 fallback 已被数组级
    fallback + `gemini_native` provider 正式取代 —— 备用 Google endpoint 现应
    作为一条独立 `gemini_native` profile 配进 route.embedding_fallbacks。

    provider == 'gemini_native' 时单 profile 走 Gemini 原生 `:embedContent`
    协议 —— 与多模态摄取走同一向量空间，保证 /search 查询向量与已存媒体
    向量可比。

    P0 asymmetric retrieval：`mode`（query|document）控制 gemini-embedding-2
    的提示词前缀。优先级 = 显式参数 > `TM_EMBED_MODE` env > 'document'。
    `/search` 子进程注入 `TM_EMBED_MODE=query`，摄取子进程不注入 → document。
    openai_compatible provider 是对称模型，mode 对其为 no-op。
    """
    chain = _resolve_chain_for_worker()
    # 显式参数最高优先；其次 env（server 端按调用语义注入）；兜底 document。
    effective_mode = mode or os.environ.get('TM_EMBED_MODE') or 'document'

    def _executor(profile) -> np.ndarray:
        return _embed_text_single(profile, text, effective_mode, title)

    return model_fallback.run_with_fallback_sync(
        model_fallback.CATEGORY_EMBED, chain, _executor,
    )
