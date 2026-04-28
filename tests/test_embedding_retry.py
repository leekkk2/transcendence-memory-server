"""embed_text 重试逻辑：429/5xx 走指数退避并尊重 Retry-After，4xx 直接抛。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://upstream.test/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "test-embed")
    # 把退避调到极小值避免测试拖慢
    monkeypatch.setenv("EMBEDDING_MAX_RETRIES", "4")
    monkeypatch.setenv("EMBEDDING_RETRY_BASE_DELAY", "0.01")
    monkeypatch.setenv("EMBEDDING_RETRY_MAX_DELAY", "0.05")
    sys.modules.pop("scripts.task_rag_runtime", None)
    sys.modules.pop("task_rag_runtime", None)
    return importlib.import_module("scripts.task_rag_runtime")


class _Resp:
    def __init__(self, status: int, json_data=None, text: str = "", headers=None):
        self.status_code = status
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


def _ok_response():
    return _Resp(200, json_data={"data": [{"embedding": [0.1, 0.2, 0.3]}]})


def test_retry_succeeds_after_5xx(runtime, monkeypatch):
    """前 2 次 500，第 3 次成功 → 期望返回向量且共调用 3 次。"""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            return _Resp(500, text="rate limited upstream")
        return _ok_response()

    monkeypatch.setattr(runtime.requests, "post", fake_post)
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: None)

    vec = runtime.embed_text("hello")
    assert isinstance(vec, np.ndarray)
    assert len(calls) == 3


def test_retry_respects_retry_after(runtime, monkeypatch):
    """429 + Retry-After=0.04 应至少等 retry_after。"""
    calls = []
    sleeps = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return _Resp(429, headers={"Retry-After": "0.04"}, text="rate limit")
        return _ok_response()

    monkeypatch.setattr(runtime.requests, "post", fake_post)
    monkeypatch.setattr(runtime.time, "sleep", lambda d: sleeps.append(d))

    runtime.embed_text("hello")
    assert len(calls) == 2
    assert sleeps and sleeps[0] >= 0.04


def test_4xx_does_not_retry(runtime, monkeypatch):
    """401/400 类客户端错误必须立即抛出，不重试。"""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp(401, text="unauthorized")

    monkeypatch.setattr(runtime.requests, "post", fake_post)
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: None)

    with pytest.raises(requests.HTTPError):
        runtime.embed_text("hello")
    assert len(calls) == 1


def test_retry_exhausted_raises(runtime, monkeypatch):
    """重试用尽后抛出聚合错误。"""
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp(503, text="upstream down")

    monkeypatch.setattr(runtime.requests, "post", fake_post)
    monkeypatch.setattr(runtime.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="failed after"):
        runtime.embed_text("hello")
    assert len(calls) == 4  # EMBEDDING_MAX_RETRIES=4
