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


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'

# Google native endpoint 模板（fallback path 用，本身不依赖 profile）。
GOOGLE_BASE_URL = os.getenv(
    'GOOGLE_EMBEDDING_BASE_URL',
    'https://generativelanguage.googleapis.com/v1beta/models',
)

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


def _resolve_profile_for_worker():
    """根据 CONTAINER / TM_EMBEDDING_PROFILE_OVERRIDE env 拿 registry profile。

    优先级（高 → 低）：
      1. TM_EMBEDDING_PROFILE_OVERRIDE — γ 的 server 端点 per-request override 注入
      2. CONTAINER env → registry.resolve(container).embedding
      3. TM_WORKER_CONTAINER env（旧别名）
      4. 缺失时走 registry.default_route — 向后兼容 legacy env-only 部署
         （这条 fallback 保证旧测试 / 旧 docker-compose 不指定 CONTAINER 时仍能工作）
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
        return registry.get_profile(override)

    container = os.environ.get('CONTAINER') or os.environ.get('TM_WORKER_CONTAINER')
    if container:
        route = registry.resolve(container)
        return registry.get_profile(route.embedding)

    # 缺失 → 用 default route（registry 总有一个 default，legacy env 路径会合成 'legacy' profile）
    return registry.get_profile(registry._profiles.default_route.embedding)


def embed_text(text: str) -> np.ndarray:
    """单条文本 embedding 调用，专供 worker 使用。

    路由：CONTAINER env -> registry.resolve -> profile -> /embeddings 调用。
    fallback：若 profile.api_key 以 'AIza' 开头（个人 Google API Key），
    在 OpenAI-style 调用全部失败后改走 Google native endpoint。

    provider == 'gemini_native' 时改走 Gemini 原生 `:embedContent` 协议
    （单 text part）—— 与多模态摄取走同一向量空间，保证 /search 查询向量
    与已存媒体向量可比。
    """
    profile = _resolve_profile_for_worker()

    if getattr(profile, 'provider', '') == 'gemini_native':
        try:
            from gemini_native_embed import embed_parts_sync, text_part  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - package import path
            from scripts.gemini_native_embed import (  # type: ignore[import-not-found]
                embed_parts_sync,
                text_part,
            )
        return embed_parts_sync(profile, [text_part(text)])

    api_key = profile.api_key
    if not api_key:
        raise RuntimeError(
            f"profile {profile.name!r} has no api_key (check api_key_env)"
        )

    base_url = profile.base_url
    model = profile.model
    url = f'{base_url.rstrip("/")}/embeddings'
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload: dict = {'model': model, 'input': text}
    # Matryoshka：profile.request_dim 指定时传 OpenAI `dimensions=N`
    request_dim = getattr(profile, 'request_dim', None)
    if request_dim:
        payload['dimensions'] = int(request_dim)

    last_err: Exception | None = None
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
            elif status >= 400:
                # 4xx（鉴权 / 参数错误）不可重试，立即抛出
                response.raise_for_status()
            else:
                data = response.json()
                return np.array(data['data'][0]['embedding'], dtype='float32')
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
        except requests.HTTPError:
            # 已被分类，直接抛
            raise
        except Exception as exc:  # JSON 解析失败、空 data 等也算可重试
            last_err = exc

        if attempt == _EMBED_MAX_RETRIES - 1:
            break
        delay = _backoff_delay(attempt, retry_after)
        logger.warning(
            'embed_text attempt %d/%d failed: %s; retrying in %.1fs',
            attempt + 1, _EMBED_MAX_RETRIES, last_err, delay,
        )
        time.sleep(delay)

    # Google native fallback：现有 special path，保留语义
    if api_key.startswith('AIza'):
        google_url = f'{GOOGLE_BASE_URL.rstrip("/")}/{model}:embedContent?key={api_key}'
        google_payload = {'content': {'parts': [{'text': text}]}}
        response = requests.post(google_url, json=google_payload, timeout=_EMBED_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return np.array(data['embedding']['values'], dtype='float32')

    raise RuntimeError(f'Embedding request failed after {_EMBED_MAX_RETRIES} retries: {last_err}')
