#!/usr/bin/env python3
"""gemini_native embedding provider — Google Gemini 原生 :embedContent 协议。

为什么单独成模块：`openai_compatible` provider 走 `/v1/embeddings`，请求体只接受
纯文本 `input`；而 Gemini 原生多模态 embedding（图片 / PDF / 视频 / 音频）只能经
Gemini 原生 `:embedContent` 端点，请求体用 `content.parts` + `inline_data`。两套
协议差异大到不宜塞进 `embedding_registry` / `task_rag_runtime`，独立成模块便于
sync（worker subprocess）与 async（server 端点 / registry）两条调用路径复用。

base_url 约定：填 Gemini relay（gemini-balance 等）的「主机根」，**不带** `/v1beta`、
**不带** `/v1`。代码统一拼 `{base_url}/v1beta/models/{model}:embedContent`。
鉴权：`x-goog-api-key` header —— token 不进 URL，不落访问日志 / 报错回显。
output_dimensionality：`profile.request_dim` 非空时透传（MRL 维度裁剪）。

R8：本模块为通用开源代码，不含任何 endpoint / token / 主机名硬编码 —— 这些只经
运行期 profiles.yaml（base_url）+ env（api_key_env 间接引用）注入。
"""
from __future__ import annotations

import base64
import logging
import mimetypes
import random
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 退避参数 —— 与 embedding_registry 保持同量级，gemini relay 限速时指数退避。
_RETRY_BASE_DELAY_S = 1.5
_RETRY_MAX_DELAY_S = 30.0

# Gemini 原生多模态支持的 MIME（见 lane 间共享契约 §固定参数）。
_EXT_MIME: dict[str, str] = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.pdf': 'application/pdf',
    '.mp4': 'video/mp4',
    '.mpeg': 'video/mpeg',
    '.mpg': 'video/mpeg',
    '.mp3': 'audio/mp3',
    '.wav': 'audio/wav',
}
SUPPORTED_MIMES = frozenset(_EXT_MIME.values())

# MIME → 粗粒度模态标签，写入 row metadata 便于检索端按类型过滤 / 展示。
_MIME_MODALITY: dict[str, str] = {
    'image/png': 'image',
    'image/jpeg': 'image',
    'application/pdf': 'pdf',
    'video/mp4': 'video',
    'video/mpeg': 'video',
    'audio/mp3': 'audio',
    'audio/wav': 'audio',
}


def guess_mime(filename: str, declared: str | None = None) -> str:
    """推断媒体 MIME。

    优先 `declared`（HTTP Content-Type，若归一后在支持集内），否则按扩展名查表，
    再否则用 `mimetypes` 兜底。返回值不保证在 `SUPPORTED_MIMES` 内 —— 调用方负责
    对不支持类型显式报错（这样错误信息能区分「认不出」与「不支持」）。
    """
    if declared:
        d = declared.split(';')[0].strip().lower()
        # audio/mpeg 是 .mp3 的标准 MIME，Gemini 侧用 audio/mp3，这里归一
        if d == 'audio/mpeg':
            d = 'audio/mp3'
        if d in SUPPORTED_MIMES:
            return d
    ext = ('.' + filename.rsplit('.', 1)[-1].lower()) if '.' in filename else ''
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return (guessed or 'application/octet-stream').lower()


def modality_of(mime: str) -> str:
    """MIME → 模态标签（image/pdf/video/audio）；未知归 'other'。"""
    return _MIME_MODALITY.get(mime, 'other')


def text_part(text: str) -> dict[str, Any]:
    """构造文本 part。"""
    return {'text': text}


def inline_part(mime: str, raw: bytes) -> dict[str, Any]:
    """构造 inline_data part —— 媒体原始字节 base64 内联。"""
    return {
        'inline_data': {
            'mime_type': mime,
            'data': base64.b64encode(raw).decode('ascii'),
        }
    }


def _model_path(model: str) -> str:
    """Gemini API 要求 model 形如 `models/<id>`；已带前缀则原样返回。"""
    return model if model.startswith('models/') else f'models/{model}'


def _endpoint(base_url: str, model: str, method: str) -> str:
    """拼 Gemini 原生端点 URL。method = embedContent | batchEmbedContents。"""
    return f"{base_url.rstrip('/')}/v1beta/{_model_path(model)}:{method}"


def _headers(api_key: str) -> dict[str, str]:
    return {'Content-Type': 'application/json', 'x-goog-api-key': api_key}


def _single_body(profile: Any, parts: list[dict[str, Any]]) -> dict[str, Any]:
    """单 content 请求体 —— 同时复用为 batchEmbedContents 的单条 request。"""
    body: dict[str, Any] = {
        'model': _model_path(profile.model),
        'content': {'parts': parts},
    }
    request_dim = getattr(profile, 'request_dim', None)
    if request_dim:
        # 契约约定字段名 output_dimensionality（gemini-balance relay 负责透传）
        body['output_dimensionality'] = int(request_dim)
    return body


def _parse_single(data: dict[str, Any]) -> list[float]:
    """解析 embedContent 响应 `{"embedding": {"values": [...]}}`。"""
    try:
        values = data['embedding']['values']
    except (KeyError, TypeError):
        raise ValueError(
            f'gemini embedContent response missing embedding.values: {str(data)[:200]}'
        )
    if not isinstance(values, list) or not values:
        raise ValueError('gemini embedContent returned empty embedding')
    return values


def _parse_batch(data: dict[str, Any], expect: int) -> np.ndarray:
    """解析 batchEmbedContents 响应 `{"embeddings": [{"values": [...]}, ...]}`。"""
    embeddings = data.get('embeddings')
    if not isinstance(embeddings, list) or len(embeddings) != expect:
        raise ValueError(
            f'gemini batchEmbedContents expected {expect} embeddings, got {str(data)[:200]}'
        )
    out: list[list[float]] = []
    for entry in embeddings:
        values = (entry or {}).get('values')
        if not isinstance(values, list) or not values:
            raise ValueError('gemini batchEmbedContents returned empty embedding')
        out.append(values)
    return np.array(out, dtype='float32')


def _retryable(status: int) -> bool:
    """429 限流 / 5xx 上游故障可重试；其余 4xx 是配置/输入错，立即抛。"""
    return status == 429 or status >= 500


def _backoff(attempt: int) -> float:
    base = min(_RETRY_BASE_DELAY_S * (2 ** attempt), _RETRY_MAX_DELAY_S)
    return max(0.5, base + base * random.uniform(-0.25, 0.25))


def embed_parts_sync(profile: Any, parts: list[dict[str, Any]]) -> np.ndarray:
    """单 content embedding —— worker subprocess 同步路径（requests）。

    parts 可含 text / inline_data，混合即多模态联合 embedding。
    返回 1-D ndarray (dim,)。
    """
    import requests

    url = _endpoint(profile.base_url, profile.model, 'embedContent')
    body = _single_body(profile, parts)
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    for attempt in range(profile.max_retries):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=profile.timeout_s)
            if _retryable(resp.status_code):
                last_err = RuntimeError(f'gemini upstream {resp.status_code}: {resp.text[:200]!r}')
            elif resp.status_code >= 400:
                resp.raise_for_status()
            else:
                return np.array(_parse_single(resp.json()), dtype='float32')
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_err = exc
        except requests.HTTPError:
            raise
        except Exception as exc:  # JSON 解析失败 / 空 values 等也算可重试
            last_err = exc
        if attempt < profile.max_retries - 1:
            delay = _backoff(attempt)
            logger.warning('gemini embed sync attempt %d failed: %s; retry in %.1fs',
                            attempt + 1, last_err, delay)
            time.sleep(delay)
    raise RuntimeError(
        f'gemini embedContent failed after {profile.max_retries} retries: {last_err}'
    )


async def embed_parts_async(profile: Any, parts: list[dict[str, Any]]) -> np.ndarray:
    """单 content embedding —— server 端点 async 路径（httpx）。返回 1-D ndarray。"""
    import asyncio

    import httpx

    url = _endpoint(profile.base_url, profile.model, 'embedContent')
    body = _single_body(profile, parts)
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if _retryable(resp.status_code):
                    last_err = RuntimeError(
                        f'gemini upstream {resp.status_code}: {resp.text[:200]!r}')
                elif resp.status_code >= 400:
                    resp.raise_for_status()
                else:
                    return np.array(_parse_single(resp.json()), dtype='float32')
            except httpx.HTTPStatusError:
                raise
            except (httpx.TransportError, ValueError) as exc:
                last_err = exc
            if attempt < profile.max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
    raise RuntimeError(
        f'gemini embedContent failed after {profile.max_retries} retries: {last_err}'
    )


async def embed_texts_async(profile: Any, texts: list[str]) -> np.ndarray:
    """批量纯文本 embedding —— registry fallback chain / probe 走这条。

    用 batchEmbedContents 一次 HTTP 调用拿 n 条向量。返回 ndarray (n, dim)。
    """
    import asyncio

    import httpx

    if not texts:
        return np.zeros((0, profile.dim), dtype='float32')
    url = _endpoint(profile.base_url, profile.model, 'batchEmbedContents')
    body = {'requests': [_single_body(profile, [text_part(t)]) for t in texts]}
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if _retryable(resp.status_code):
                    last_err = RuntimeError(
                        f'gemini upstream {resp.status_code}: {resp.text[:200]!r}')
                elif resp.status_code >= 400:
                    resp.raise_for_status()
                else:
                    return _parse_batch(resp.json(), expect=len(texts))
            except httpx.HTTPStatusError:
                raise
            except (httpx.TransportError, ValueError) as exc:
                last_err = exc
            if attempt < profile.max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
    raise RuntimeError(
        f'gemini batchEmbedContents failed after {profile.max_retries} retries: {last_err}'
    )
