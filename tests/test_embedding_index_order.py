"""D1 回归：批量 embedding 响应 data[].index 为 null/缺失/乱序时的排序行为。

真实形态来源：OpenAI 兼容聚合网关实测返回 ``index: null``（单条全 null、
批量 null 与 int 混杂两种形态均出现过），旧实现 ``sorted(key=x["index"])``
直接 TypeError → 批量预计算 embedding 整体失败 → hybrid 检索 0 vector chunks。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.embedding_registry as reg_mod
from scripts.embedding_registry import _order_embedding_data
from scripts.profiles_loader import EmbeddingProfile


# ---- _order_embedding_data 纯函数 ----------------------------------------

def test_all_int_indices_sorted_by_index():
    data = [
        {"index": 1, "embedding": [1.0]},
        {"index": 0, "embedding": [0.0]},
    ]
    assert [d["embedding"] for d in _order_embedding_data(data)] == [[0.0], [1.0]]


def test_null_index_falls_back_to_response_order():
    data = [
        {"index": None, "embedding": [0.0]},
        {"index": None, "embedding": [1.0]},
    ]
    assert [d["embedding"] for d in _order_embedding_data(data)] == [[0.0], [1.0]]


def test_missing_index_key_falls_back_to_response_order():
    data = [{"embedding": [0.0]}, {"embedding": [1.0]}]
    assert [d["embedding"] for d in _order_embedding_data(data)] == [[0.0], [1.0]]


def test_mixed_null_and_int_falls_back_to_response_order():
    # 真实网关 "double" 形态：[{index: null}, {index: 1}]
    data = [
        {"index": None, "embedding": [0.0]},
        {"index": 1, "embedding": [1.0]},
    ]
    assert [d["embedding"] for d in _order_embedding_data(data)] == [[0.0], [1.0]]


# ---- 经 _http_embed_single 全链路（含 JSON 解析） --------------------------

def _profile(dim: int = 2) -> EmbeddingProfile:
    return EmbeddingProfile(
        name="gw",
        provider="openai_compatible",
        model="test-embedding-model",
        dim=dim,
        base_url="https://llm-gateway.example/v1",
        api_key="test-key",
        max_retries=1,
    )


def _patch_transport(monkeypatch, payload: dict) -> None:
    """让 _http_embed_single 内部新建的 AsyncClient 走 MockTransport。"""
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload))

    def factory(**kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_client(**kwargs)

    monkeypatch.setattr(reg_mod.httpx, "AsyncClient", factory)


def test_http_embed_single_survives_null_index(monkeypatch):
    payload = {
        "data": [
            {"index": None, "embedding": [0.0, 0.0]},
            {"index": None, "embedding": [1.0, 1.0]},
        ],
    }
    _patch_transport(monkeypatch, payload)
    result = asyncio.run(reg_mod._http_embed_single(_profile(), ["a", "b"]))
    np.testing.assert_array_equal(
        result, np.array([[0.0, 0.0], [1.0, 1.0]], dtype="float32")
    )


def test_http_embed_single_sorts_when_indices_out_of_order(monkeypatch):
    payload = {
        "data": [
            {"index": 1, "embedding": [1.0, 1.0]},
            {"index": 0, "embedding": [0.0, 0.0]},
        ],
    }
    _patch_transport(monkeypatch, payload)
    result = asyncio.run(reg_mod._http_embed_single(_profile(), ["a", "b"]))
    np.testing.assert_array_equal(
        result, np.array([[0.0, 0.0], [1.0, 1.0]], dtype="float32")
    )


def test_http_embed_single_mixed_indices_uses_response_order(monkeypatch):
    payload = {
        "data": [
            {"index": None, "embedding": [0.0, 0.0]},
            {"index": 1, "embedding": [1.0, 1.0]},
        ],
    }
    _patch_transport(monkeypatch, payload)
    result = asyncio.run(reg_mod._http_embed_single(_profile(), ["a", "b"]))
    np.testing.assert_array_equal(
        result, np.array([[0.0, 0.0], [1.0, 1.0]], dtype="float32")
    )
