"""gemini_native embedding provider 单测。

覆盖：MIME 推断、part 构造、请求体 / 响应解析、sync+async+batch 三条调用路径的
成功、重试（429/5xx）与不可重试（4xx）行为。HTTP 全程 mock，不打真实上游。
"""
from __future__ import annotations

import asyncio
import base64
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import gemini_native_embed as gne  # noqa: E402


@dataclass
class _Profile:
    """gemini_native_embed 只依赖 EmbeddingProfile 的 duck-typed 子集。"""

    model: str = "gemini-embedding-2"
    dim: int = 3072
    base_url: str = "https://relay.test"
    api_key: str = "tok-123"
    timeout_s: float = 30.0
    max_retries: int = 3
    request_dim: int | None = None


# ---- MIME / part 构造 ----------------------------------------------------

def test_guess_mime_prefers_supported_declared():
    assert gne.guess_mime("x.bin", "image/png") == "image/png"


def test_guess_mime_normalizes_audio_mpeg():
    assert gne.guess_mime("clip", "audio/mpeg") == "audio/mp3"


def test_guess_mime_falls_back_to_extension():
    assert gne.guess_mime("doc.pdf", None) == "application/pdf"
    assert gne.guess_mime("a.JPG", "application/octet-stream") == "image/jpeg"


def test_modality_of():
    assert gne.modality_of("image/jpeg") == "image"
    assert gne.modality_of("application/pdf") == "pdf"
    assert gne.modality_of("video/mp4") == "video"
    assert gne.modality_of("audio/wav") == "audio"
    assert gne.modality_of("text/plain") == "other"


def test_inline_part_base64_roundtrip():
    raw = b"\x00\x01media-bytes"
    part = gne.inline_part("image/png", raw)
    assert part["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(part["inline_data"]["data"]) == raw


# ---- 请求体 / 响应解析 ---------------------------------------------------

def test_single_body_adds_model_prefix_and_dim():
    body = gne._single_body(_Profile(request_dim=1024), [gne.text_part("hi")])
    assert body["model"] == "models/gemini-embedding-2"
    assert body["content"]["parts"] == [{"text": "hi"}]
    assert body["output_dimensionality"] == 1024


def test_single_body_omits_dim_when_unset():
    assert "output_dimensionality" not in gne._single_body(_Profile(), [])


def test_parse_single_ok_and_errors():
    assert gne._parse_single({"embedding": {"values": [0.1, 0.2]}}) == [0.1, 0.2]
    with pytest.raises(ValueError):
        gne._parse_single({"embedding": {}})
    with pytest.raises(ValueError):
        gne._parse_single({"embedding": {"values": []}})


def test_parse_batch_ok_and_count_mismatch():
    data = {"embeddings": [{"values": [1.0]}, {"values": [2.0]}]}
    out = gne._parse_batch(data, expect=2)
    assert out.shape == (2, 1)
    with pytest.raises(ValueError):
        gne._parse_batch(data, expect=3)


# ---- HTTP mock 基建 ------------------------------------------------------

class _Resp:
    def __init__(self, status, json_data=None, text=""):
        self.status_code = status
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            err = requests.HTTPError(str(self.status_code))
            err.response = self
            raise err


# ---- embed_parts_sync ----------------------------------------------------

def test_embed_parts_sync_success(monkeypatch):
    import requests
    monkeypatch.setattr(gne.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        requests, "post",
        lambda *a, **k: _Resp(200, {"embedding": {"values": [0.1] * 3072}}),
    )
    vec = gne.embed_parts_sync(_Profile(), [gne.text_part("probe")])
    assert vec.shape == (3072,)


def test_embed_parts_sync_retries_then_succeeds(monkeypatch):
    import requests
    monkeypatch.setattr(gne.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(503, text="upstream down")
        return _Resp(200, {"embedding": {"values": [0.5, 0.6]}})

    monkeypatch.setattr(requests, "post", fake_post)
    vec = gne.embed_parts_sync(_Profile(max_retries=3), [gne.text_part("x")])
    assert calls["n"] == 3
    assert list(vec) == pytest.approx([0.5, 0.6])


def test_embed_parts_sync_4xx_raises_without_retry(monkeypatch):
    import requests
    monkeypatch.setattr(gne.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _Resp(400, text="bad request")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(requests.HTTPError):
        gne.embed_parts_sync(_Profile(max_retries=3), [gne.text_part("x")])
    assert calls["n"] == 1  # 4xx 不重试


# ---- async 路径 ----------------------------------------------------------

class _FakeAsyncClient:
    """最小 httpx.AsyncClient 替身：按预置 _Resp 序列依次返回。"""

    responses: list = []
    sent: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.sent.append({"url": url, "json": json})
        return _FakeAsyncClient.responses.pop(0)


def test_embed_parts_async_success(monkeypatch):
    import httpx
    _FakeAsyncClient.responses = [_Resp(200, {"embedding": {"values": [0.2] * 3072}})]
    _FakeAsyncClient.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    vec = asyncio.run(gne.embed_parts_async(_Profile(), [gne.text_part("hi")]))
    assert vec.shape == (3072,)
    assert _FakeAsyncClient.sent[0]["url"].endswith(
        "/v1beta/models/gemini-embedding-2:embedContent"
    )


def test_embed_texts_async_batch(monkeypatch):
    import httpx
    _FakeAsyncClient.responses = [
        _Resp(200, {"embeddings": [{"values": [1.0, 1.1]}, {"values": [2.0, 2.1]}]})
    ]
    _FakeAsyncClient.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    out = asyncio.run(gne.embed_texts_async(_Profile(), ["a", "b"]))
    assert out.shape == (2, 2)
    assert _FakeAsyncClient.sent[0]["url"].endswith(":batchEmbedContents")
    assert len(_FakeAsyncClient.sent[0]["json"]["requests"]) == 2


def test_embed_texts_async_empty_shortcuts():
    out = asyncio.run(gne.embed_texts_async(_Profile(), []))
    assert out.shape == (0, 3072)


# ---- P0：asymmetric retrieval prefix ------------------------------------

def test_query_text_prefix():
    assert gne.query_text("猫在哪") == "task: search result | query: 猫在哪"


def test_document_text_prefix():
    assert gne.document_text("body", "T") == "title: T | text: body"
    assert gne.document_text("body") == "title: none | text: body"


def test_apply_retrieval_mode_query_wraps_text_only():
    parts = [gne.text_part("q"), gne.inline_part("image/png", b"x")]
    out = gne._apply_retrieval_mode(parts, "query", None)
    assert out[0] == {"text": "task: search result | query: q"}
    # 媒体 part 任何 mode 都原样保留
    assert out[1] == parts[1]


def test_apply_retrieval_mode_document_uses_title():
    out = gne._apply_retrieval_mode([gne.text_part("c")], "document", "doc.pdf")
    assert out[0] == {"text": "title: doc.pdf | text: c"}


def test_apply_retrieval_mode_none_and_media_passthrough():
    parts = [gne.text_part("c")]
    assert gne._apply_retrieval_mode(parts, None, None) == parts
    assert gne._apply_retrieval_mode(parts, "media", None) == parts


def test_apply_retrieval_mode_rejects_unknown():
    with pytest.raises(ValueError):
        gne._apply_retrieval_mode([gne.text_part("c")], "bogus", None)


def test_embed_parts_sync_applies_query_prefix(monkeypatch):
    import requests
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["body"] = json
        return _Resp(200, {"embedding": {"values": [0.1] * 3072}})

    monkeypatch.setattr(requests, "post", fake_post)
    gne.embed_parts_sync(_Profile(), [gne.text_part("hello")], mode="query")
    assert captured["body"]["content"]["parts"] == [
        {"text": "task: search result | query: hello"}
    ]


def test_embed_texts_async_applies_document_prefix(monkeypatch):
    import httpx
    _FakeAsyncClient.responses = [
        _Resp(200, {"embeddings": [{"values": [1.0]}]})
    ]
    _FakeAsyncClient.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    asyncio.run(gne.embed_texts_async(_Profile(), ["body"], mode="document", title="T"))
    req0 = _FakeAsyncClient.sent[0]["json"]["requests"][0]
    assert req0["content"]["parts"] == [{"text": "title: T | text: body"}]


# ---- P1：媒体 caption 生成 ----------------------------------------------

def test_parse_caption_ok_and_errors():
    data = {"candidates": [{"content": {"parts": [{"text": "a cat"}]}}]}
    assert gne._parse_caption(data) == "a cat"
    with pytest.raises(ValueError):
        gne._parse_caption({"candidates": []})
    with pytest.raises(ValueError):
        gne._parse_caption({"candidates": [{"content": {"parts": [{"text": ""}]}}]})


def test_generate_caption_endpoint():
    url = gne._generate_content_endpoint("https://relay.test", "gemini-2.5-flash-lite")
    assert url == (
        "https://relay.test/v1beta/models/gemini-2.5-flash-lite:generateContent"
    )


def test_generate_media_caption_async_success(monkeypatch):
    import httpx
    _FakeAsyncClient.responses = [
        _Resp(200, {"candidates": [{"content": {"parts": [{"text": "a red car"}]}}]})
    ]
    _FakeAsyncClient.sent = []
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    cap = asyncio.run(
        gne.generate_media_caption_async(_Profile(), "image/jpeg", b"bytes")
    )
    assert cap == "a red car"
    sent = _FakeAsyncClient.sent[0]
    assert sent["url"].endswith(":generateContent")
    # 媒体 part + prompt part
    parts = sent["json"]["contents"][0]["parts"]
    assert "inline_data" in parts[0] and "text" in parts[1]
