#!/usr/bin/env python3
"""embedding 错误类型的向后兼容薄壳 —— 真正实现已泛化迁入 ``model_errors``。

历史代码（``embed_backlog`` / ``index_state`` / ``task_rag_lancedb_ingest`` 等）
直接 ``from embedding_errors import EmbeddingError`` / ``classify_embedding_error``。
本模块把这些旧名**直接别名**到 ``model_errors`` 的泛化实现 —— 别名两侧是同一
class object，``except EmbeddingError`` / ``isinstance(exc, EmbeddingError)`` 在
升级后仍然成立。

R8：纯通用开源代码，不含任何 endpoint / 主机名 / 凭证。
"""
from __future__ import annotations

import sys as _sys

# 模块身份归一：worker subprocess（bare import）与 server（package import）
# 两种入口必须共用同一份 model_errors 类对象，否则 typed exception 的跨副本
# isinstance 会失效。先复用任一已加载副本，再登记到两个模块名下。
_model_errors = (
    _sys.modules.get('scripts.model_errors')
    or _sys.modules.get('model_errors')
)
if _model_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import model_errors as _model_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import model_errors as _model_errors  # type: ignore[no-redef]
_sys.modules.setdefault('model_errors', _model_errors)
_sys.modules.setdefault('scripts.model_errors', _model_errors)

from model_errors import (  # noqa: E402  泛化实现的全部公共名
    ALL_CLASSES,
    AUTH,
    PERMANENT,
    QUOTA,
    RETRYABLE_CLASSES,
    TIMEOUT,
    TRANSIENT,
    ModelAuthError,
    ModelError,
    ModelPermanentError,
    ModelQuotaError,
    ModelTimeoutError,
    ModelTransientError,
    classify_model_error,
    classify_status,
    is_context_overflow,
    is_retryable,
    make_model_error,
    parse_retry_after,
)

# ---- 旧名直接别名（与 model_errors 共享同一 class object / 同一函数对象）---
EmbeddingError = ModelError
EmbeddingQuotaError = ModelQuotaError
EmbeddingTimeoutError = ModelTimeoutError
EmbeddingTransientError = ModelTransientError
EmbeddingPermanentError = ModelPermanentError
classify_embedding_error = classify_model_error
make_embedding_error = make_model_error

__all__ = [
    'QUOTA', 'TIMEOUT', 'TRANSIENT', 'PERMANENT', 'AUTH',
    'RETRYABLE_CLASSES', 'ALL_CLASSES',
    'ModelError', 'ModelQuotaError', 'ModelTimeoutError',
    'ModelTransientError', 'ModelPermanentError', 'ModelAuthError',
    'EmbeddingError', 'EmbeddingQuotaError', 'EmbeddingTimeoutError',
    'EmbeddingTransientError', 'EmbeddingPermanentError',
    'parse_retry_after', 'classify_status', 'classify_model_error',
    'classify_embedding_error', 'is_context_overflow', 'is_retryable',
    'make_model_error', 'make_embedding_error',
]

# 本薄壳自身也登记到两个模块名下，保持与旧版 embedding_errors 同样的可双路
# import 行为（部分调用点 bare import，部分走 scripts. 前缀）。
_sys.modules.setdefault('embedding_errors', _sys.modules[__name__])
_sys.modules.setdefault('scripts.embedding_errors', _sys.modules[__name__])
