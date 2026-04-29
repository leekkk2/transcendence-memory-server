#!/usr/bin/env python3
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


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'

API_KEY = os.getenv('EMBEDDING_API_KEY', '')
MODEL = os.getenv('EMBEDDING_MODEL', 'gemini-embedding-001')
EMBEDDINGS_BASE_URL = (
    os.getenv('EMBEDDING_BASE_URL')
    or os.getenv('EMBEDDINGS_BASE_URL')
    or 'https://api.openai.com/v1'
)
GOOGLE_BASE_URL = os.getenv('GOOGLE_EMBEDDING_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/models')

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


def embed_text(text: str) -> np.ndarray:
    if not API_KEY:
        raise RuntimeError('EMBEDDING_API_KEY not set')

    url = f'{EMBEDDINGS_BASE_URL.rstrip("/")}/embeddings'
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {'model': MODEL, 'input': text}

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

    if API_KEY.startswith('AIza'):
        google_url = f'{GOOGLE_BASE_URL.rstrip("/")}/{MODEL}:embedContent?key={API_KEY}'
        google_payload = {'content': {'parts': [{'text': text}]}}
        response = requests.post(google_url, json=google_payload, timeout=_EMBED_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return np.array(data['embedding']['values'], dtype='float32')

    raise RuntimeError(f'Embedding request failed after {_EMBED_MAX_RETRIES} retries: {last_err}')
