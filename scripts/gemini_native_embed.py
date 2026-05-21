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
import os
import random
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# 双重 import + 模块身份归一。worker subprocess（sys.path[0]=scripts，bare
# import）与 server/package（scripts. 前缀）两种入口可能各加载一份
# embedding_errors 副本 —— typed exception 的 isinstance 跨副本会失效。这里
# 复用任一已加载副本，并登记到两个模块名下，保证全进程只有一份类对象。
import sys as _sys

embedding_errors = (
    _sys.modules.get('scripts.embedding_errors')
    or _sys.modules.get('embedding_errors')
)
if embedding_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import embedding_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import embedding_errors  # type: ignore[no-redef]
_sys.modules.setdefault('embedding_errors', embedding_errors)
_sys.modules.setdefault('scripts.embedding_errors', embedding_errors)

# 退避参数 —— 与 embedding_registry 保持同量级，gemini relay 限速时指数退避。
_RETRY_BASE_DELAY_S = 1.5
_RETRY_MAX_DELAY_S = 30.0

# Gemini 原生多模态支持的 MIME。
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


# ---- asymmetric retrieval prefix（P0）-----------------------------------
# gemini-embedding-2 是指令微调模型，**不支持** `task_type` 参数 —— 必须用提示词
# 前缀对齐查询/文档语义。缺前缀时检索精度（尤其「文本查媒体」）明显下降。
# 来源：ai.google.dev/gemini-api/docs/embeddings。
_QUERY_TASK = 'search result'


def query_text(text: str) -> str:
    """查询侧文本 —— 套 asymmetric query 前缀。"""
    return f'task: {_QUERY_TASK} | query: {text}'


def document_text(text: str, title: str | None = None) -> str:
    """文档侧文本 —— 套 asymmetric document 前缀；title 缺省填 none。"""
    return f"title: {title or 'none'} | text: {text}"


def _apply_retrieval_mode(
    parts: list[dict[str, Any]], mode: str | None, title: str | None,
) -> list[dict[str, Any]]:
    """按 mode 给 text part 套 asymmetric retrieval 前缀。

    - mode='query'      → text part 包 query 前缀
    - mode='document'   → text part 包 document 前缀
    - mode=None/'media' → 原样返回（媒体直 embed，或调用方已自行格式化）

    `inline_data` 媒体 part 在任何 mode 下都原样保留 —— 官方未定义媒体侧前缀，
    闭合「文本查媒体」差距靠的是查询侧带前缀（见接入指南 §2.1）。
    """
    if mode in (None, 'media'):
        return parts
    if mode not in ('query', 'document'):
        raise ValueError(f'unknown embedding mode {mode!r} (expect query|document)')
    out: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, dict) and 'text' in part and 'inline_data' not in part:
            raw = part['text']
            wrapped = query_text(raw) if mode == 'query' else document_text(raw, title)
            out.append({'text': wrapped})
        else:
            out.append(part)
    return out


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


def embed_parts_sync(
    profile: Any, parts: list[dict[str, Any]],
    mode: str | None = None, title: str | None = None,
) -> np.ndarray:
    """单 content embedding —— worker subprocess 同步路径（requests）。

    parts 可含 text / inline_data，混合即多模态联合 embedding。
    mode（query|document）非空时给 text part 套 asymmetric 前缀（P0）；
    inline_data 媒体 part 不受影响。返回 1-D ndarray (dim,)。
    """
    import requests

    parts = _apply_retrieval_mode(parts, mode, title)
    url = _endpoint(profile.base_url, profile.model, 'embedContent')
    body = _single_body(profile, parts)
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    # 记录最后一次失败的状态码 / Retry-After / 类别 —— 重试耗尽后归类成 typed
    # exception，让 backlog 调度无需再字符串匹配。
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
    for attempt in range(profile.max_retries):
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=profile.timeout_s)
            if _retryable(resp.status_code):
                last_status = resp.status_code
                last_retry_after = embedding_errors.parse_retry_after(
                    (getattr(resp, 'headers', None) or {}).get('Retry-After'))
                last_error_class = None
                last_err = RuntimeError(f'gemini upstream {resp.status_code}: {resp.text[:200]!r}')
            elif resp.status_code >= 400:
                resp.raise_for_status()
            else:
                return np.array(_parse_single(resp.json()), dtype='float32')
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
            raise
        except Exception as exc:  # JSON 解析失败 / 空 values 等也算可重试
            last_err = exc
            last_status = None
            last_retry_after = None
            last_error_class = None
        if attempt < profile.max_retries - 1:
            delay = _backoff(attempt)
            logger.warning('gemini embed sync attempt %d failed: %s; retry in %.1fs',
                            attempt + 1, last_err, delay)
            time.sleep(delay)
    raise embedding_errors.make_embedding_error(
        f'gemini embedContent failed after {profile.max_retries} retries: {last_err}',
        status=last_status, retry_after=last_retry_after, error_class=last_error_class,
    )


async def embed_parts_async(
    profile: Any, parts: list[dict[str, Any]],
    mode: str | None = None, title: str | None = None,
) -> np.ndarray:
    """单 content embedding —— server 端点 async 路径（httpx）。

    mode（query|document）非空时给 text part 套 asymmetric 前缀（P0）；
    inline_data 媒体 part 不受影响。返回 1-D ndarray。
    """
    import asyncio

    import httpx

    parts = _apply_retrieval_mode(parts, mode, title)
    url = _endpoint(profile.base_url, profile.model, 'embedContent')
    body = _single_body(profile, parts)
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if _retryable(resp.status_code):
                    last_status = resp.status_code
                    last_retry_after = embedding_errors.parse_retry_after(
                        (getattr(resp, 'headers', None) or {}).get('Retry-After'))
                    last_error_class = None
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
                last_status = None
                last_retry_after = None
                # httpx.TimeoutException 是 TransportError 子类 —— 超时单列
                last_error_class = (
                    embedding_errors.TIMEOUT
                    if isinstance(exc, httpx.TimeoutException) else None
                )
            if attempt < profile.max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
    raise embedding_errors.make_embedding_error(
        f'gemini embedContent failed after {profile.max_retries} retries: {last_err}',
        status=last_status, retry_after=last_retry_after, error_class=last_error_class,
    )


async def embed_texts_async(
    profile: Any, texts: list[str],
    mode: str | None = None, title: str | None = None,
) -> np.ndarray:
    """批量纯文本 embedding —— registry fallback chain / probe 走这条。

    用 batchEmbedContents 一次 HTTP 调用拿 n 条向量。mode 非空时给每条文本套
    asymmetric 前缀（P0）；registry/LightRAG 路径不传 mode，保持既有行为。
    返回 ndarray (n, dim)。
    """
    import asyncio

    import httpx

    if not texts:
        return np.zeros((0, profile.dim), dtype='float32')
    url = _endpoint(profile.base_url, profile.model, 'batchEmbedContents')
    body = {
        'requests': [
            _single_body(profile, _apply_retrieval_mode([text_part(t)], mode, title))
            for t in texts
        ],
    }
    headers = _headers(profile.api_key)
    last_err: Exception | None = None
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if _retryable(resp.status_code):
                    last_status = resp.status_code
                    last_retry_after = embedding_errors.parse_retry_after(
                        (getattr(resp, 'headers', None) or {}).get('Retry-After'))
                    last_error_class = None
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
                last_status = None
                last_retry_after = None
                last_error_class = (
                    embedding_errors.TIMEOUT
                    if isinstance(exc, httpx.TimeoutException) else None
                )
            if attempt < profile.max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
    raise embedding_errors.make_embedding_error(
        f'gemini batchEmbedContents failed after {profile.max_retries} retries: {last_err}',
        status=last_status, retry_after=last_retry_after, error_class=last_error_class,
    )


# ---- 媒体 caption 生成（P1 caption 混合检索）----------------------------
# 经 Gemini relay（gemini-balance）调 vision-capable chat 模型为媒体生成文字
# caption；caption 再走 document 文本 embedding 存为兄弟行，闭合「文本查媒体」
# 的模态间隙。复用 embedding profile 的 base_url + api_key（同一 relay、同一
# 鉴权 token），避开独立 VLM 网关的可用性风险。VLM 模型 id 可经 env 覆盖。
_DEFAULT_CAPTION_MODEL = os.environ.get('GE2_CAPTION_VLM_MODEL', 'gemini-2.5-flash-lite')

_CAPTION_PROMPT = (
    'Describe this media concisely and factually for search retrieval. '
    'Cover the key visible or audible content: objects, people, actions, '
    'on-screen or spoken text, and setting. Output only the description '
    'itself with no preamble. Keep it under 80 words.'
)


def _generate_content_endpoint(base_url: str, model: str) -> str:
    """拼 Gemini 原生 generateContent 端点 URL。"""
    return f"{base_url.rstrip('/')}/v1beta/{_model_path(model)}:generateContent"


def _parse_caption(data: dict[str, Any]) -> str:
    """解析 generateContent 响应 `{"candidates":[{"content":{"parts":[...]}}]}`。"""
    try:
        parts = data['candidates'][0]['content']['parts']
    except (KeyError, IndexError, TypeError):
        raise ValueError(
            f'gemini generateContent response missing candidates: {str(data)[:200]}'
        )
    texts = [p.get('text', '') for p in parts if isinstance(p, dict) and p.get('text')]
    caption = ' '.join(t.strip() for t in texts if t.strip()).strip()
    if not caption:
        raise ValueError('gemini generateContent returned empty caption')
    return caption


async def generate_media_caption_async(
    profile: Any, mime: str, raw: bytes,
    model: str | None = None, prompt: str | None = None,
) -> str:
    """经 Gemini relay 为媒体生成文字 caption（vision chat）。

    复用 `profile.base_url`（relay 主机根）+ `profile.api_key`（relay token），
    与 embedding 同一 relay、同一鉴权。返回 caption 文本；上游失败抛异常，
    调用方按 best-effort 处理（caption 失败不应阻塞媒体行落库）。
    """
    import asyncio

    import httpx

    vlm_model = model or _DEFAULT_CAPTION_MODEL
    url = _generate_content_endpoint(profile.base_url, vlm_model)
    headers = _headers(profile.api_key)
    body = {
        'contents': [{
            'parts': [
                inline_part(mime, raw),
                text_part(prompt or _CAPTION_PROMPT),
            ],
        }],
        # 低温度求事实性、稳定的检索用描述；256 token 足够 80 词以内的 caption。
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 256},
    }
    last_err: Exception | None = None
    last_status: int | None = None
    last_retry_after: float | None = None
    last_error_class: str | None = None
    async with httpx.AsyncClient(timeout=profile.timeout_s) as client:
        for attempt in range(profile.max_retries):
            try:
                resp = await client.post(url, json=body, headers=headers)
                if _retryable(resp.status_code):
                    last_status = resp.status_code
                    last_retry_after = embedding_errors.parse_retry_after(
                        (getattr(resp, 'headers', None) or {}).get('Retry-After'))
                    last_error_class = None
                    last_err = RuntimeError(
                        f'gemini caption upstream {resp.status_code}: {resp.text[:200]!r}')
                elif resp.status_code >= 400:
                    resp.raise_for_status()
                else:
                    return _parse_caption(resp.json())
            except httpx.HTTPStatusError:
                raise
            except (httpx.TransportError, ValueError) as exc:
                last_err = exc
                last_status = None
                last_retry_after = None
                last_error_class = (
                    embedding_errors.TIMEOUT
                    if isinstance(exc, httpx.TimeoutException) else None
                )
            if attempt < profile.max_retries - 1:
                await asyncio.sleep(_backoff(attempt))
    raise embedding_errors.make_embedding_error(
        f'gemini generateContent failed after {profile.max_retries} retries: {last_err}',
        status=last_status, retry_after=last_retry_after, error_class=last_error_class,
    )
