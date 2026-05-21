#!/usr/bin/env python3
"""Embedding 错误的统一类型 + 分类器 —— embedding backlog / 重试调度的共享契约。

为什么需要：embedding 失败在历史代码里被压成裸 `RuntimeError`（消息里塞状态码），
调用方只能字符串匹配。backlog 重试调度需要按「错误类别」决定退避策略与去向：

- ``quota``      —— 上游配额耗尽（HTTP 429 / RESOURCE_EXHAUSTED）。free tier 按
                    分钟 / 按天重置，重试 cadence 拉长并尊重 Retry-After。
- ``timeout``    —— 网络 / 上游超时。中等 cadence 重试。
- ``transient``  —— 5xx / 连接抖动 / 响应损坏。短 cadence 重试。
- ``permanent``  —— 4xx 非 429（鉴权 / 参数 / 权限错）。**不自动重试**，进 dead-letter。

设计约定：

1. 所有 typed exception 都继承 ``RuntimeError`` —— 历史调用点 ``except RuntimeError``
   仍能捕获，升级不破坏行为。
2. ``classify_embedding_error`` 同时认 typed exception 与 legacy
   ``httpx.HTTPStatusError`` / ``requests.HTTPError`` / 传输错误 / ``ValueError``，
   以及历史「字符串塞状态码」的裸 ``RuntimeError`` —— hotpatch 半应用期间优雅降级。
3. **未知错误默认归 ``transient`` 而非 ``permanent``** —— 误判 transient 只多一次
   无形重试；误判 permanent 是把可恢复的内容永久判死，对长期记忆系统不可接受。

R8：纯通用开源代码，不含任何 endpoint / 主机名 / 凭证。
"""
from __future__ import annotations

import re
from email.utils import parsedate_to_datetime

# ---- 错误类别常量（backlog.error_class / 状态机共用的字符串值）-----------
QUOTA = 'quota'
TIMEOUT = 'timeout'
TRANSIENT = 'transient'
PERMANENT = 'permanent'

#: 自动重试的类别。``permanent`` 不在内 —— 它进 dead-letter 等人工介入。
RETRYABLE_CLASSES = frozenset({QUOTA, TIMEOUT, TRANSIENT})
ALL_CLASSES = frozenset({QUOTA, TIMEOUT, TRANSIENT, PERMANENT})


# ---- typed exceptions ----------------------------------------------------
class EmbeddingError(RuntimeError):
    """embedding 调用失败的基类。继承 RuntimeError 保持向后兼容。

    Attributes:
        error_class: 四类之一，决定 backlog 退避策略与去向。
        status: 上游 HTTP 状态码（若有），便于诊断。
        retry_after: 上游 ``Retry-After`` 建议的等待秒数（若有），重试调度的下界。
    """

    error_class: str = TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class EmbeddingQuotaError(EmbeddingError):
    """上游配额耗尽（429 / RESOURCE_EXHAUSTED）。"""

    error_class = QUOTA


class EmbeddingTimeoutError(EmbeddingError):
    """网络 / 上游超时。"""

    error_class = TIMEOUT


class EmbeddingTransientError(EmbeddingError):
    """5xx / 连接抖动 / 响应体损坏 —— 可短期重试。"""

    error_class = TRANSIENT


class EmbeddingPermanentError(EmbeddingError):
    """4xx 非 429（鉴权 / 参数 / 权限错）—— 不自动重试。"""

    error_class = PERMANENT


# ---- Retry-After 解析（与 embedding_registry / task_rag_runtime 同语义）---
def parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After`` 头：纯数字秒 或 RFC7231 HTTP-date 都支持。"""
    if not value:
        return None
    import time as _t

    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        return max(0.0, dt.timestamp() - _t.time())
    except (TypeError, ValueError):
        return None


# ---- 状态码 → 类别 -------------------------------------------------------
def classify_status(status: int) -> str:
    """HTTP 状态码 → 错误类别。"""
    if status == 429:
        return QUOTA
    if status == 408:
        return TIMEOUT
    if status >= 500:
        return TRANSIENT
    if status >= 400:
        return PERMANENT
    return TRANSIENT


# legacy 裸 RuntimeError 消息里塞状态码的形态，如
# ``embedding upstream 429: ...`` / ``gemini upstream 503: ...``。
_STATUS_IN_MSG = re.compile(r'\bupstream\s+(\d{3})\b')
# RESOURCE_EXHAUSTED 是 Google 配额耗尽的标志串（429 的语义别名）。
_QUOTA_HINT = re.compile(r'resource_exhausted|quota|rate.?limit', re.IGNORECASE)
_TIMEOUT_HINT = re.compile(r'timed?\s*out|timeout', re.IGNORECASE)


def classify_embedding_error(exc: BaseException) -> str:
    """把任意 embedding 失败异常归类为四类之一。

    识别顺序：typed exception → httpx → requests → 通用超时/传输 → 裸
    RuntimeError 消息启发式 → 默认 ``transient``。
    """
    # 1) typed exception —— 自带权威类别
    if isinstance(exc, EmbeddingError):
        return exc.error_class

    # 2) httpx
    try:
        import httpx

        if isinstance(exc, httpx.HTTPStatusError):
            return classify_status(exc.response.status_code)
        if isinstance(exc, httpx.TimeoutException):
            return TIMEOUT
        if isinstance(exc, httpx.TransportError):
            return TRANSIENT
    except ImportError:  # pragma: no cover - httpx 总在依赖里
        pass

    # 3) requests
    try:
        import requests

        if isinstance(exc, requests.HTTPError):
            resp = getattr(exc, 'response', None)
            if resp is not None and getattr(resp, 'status_code', None):
                return classify_status(int(resp.status_code))
            return PERMANENT
        if isinstance(exc, requests.Timeout):
            return TIMEOUT
        if isinstance(exc, requests.ConnectionError):
            return TRANSIENT
    except ImportError:  # pragma: no cover
        pass

    # 4) 通用：响应体损坏 / JSON 错 —— 当作 transient（等价上游异常）
    if isinstance(exc, ValueError):
        return TRANSIENT

    # 5) 裸 RuntimeError —— 历史代码把状态码塞进消息字符串
    msg = str(exc)
    m = _STATUS_IN_MSG.search(msg)
    if m:
        return classify_status(int(m.group(1)))
    if _QUOTA_HINT.search(msg):
        return QUOTA
    if _TIMEOUT_HINT.search(msg):
        return TIMEOUT

    # 6) 默认 transient —— 误判 transient 只多一次重试，误判 permanent 是判死
    return TRANSIENT


def is_retryable(exc_or_class: BaseException | str) -> bool:
    """异常或类别字符串是否应自动重试（非 ``permanent``）。"""
    cls = (
        exc_or_class
        if isinstance(exc_or_class, str)
        else classify_embedding_error(exc_or_class)
    )
    return cls in RETRYABLE_CLASSES


def make_embedding_error(
    message: str,
    *,
    status: int | None = None,
    retry_after: float | None = None,
    error_class: str | None = None,
) -> EmbeddingError:
    """按状态码 / 显式类别构造对应的 typed exception。

    供 embedding 调用点在重试耗尽后把失败包装成 typed exception，让上层
    backlog 调度无需再字符串匹配。
    """
    cls = error_class or (classify_status(status) if status is not None else TRANSIENT)
    factory = {
        QUOTA: EmbeddingQuotaError,
        TIMEOUT: EmbeddingTimeoutError,
        TRANSIENT: EmbeddingTransientError,
        PERMANENT: EmbeddingPermanentError,
    }.get(cls, EmbeddingTransientError)
    return factory(message, status=status, retry_after=retry_after)
