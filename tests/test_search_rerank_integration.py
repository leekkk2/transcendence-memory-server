"""Lightweight /search rerank integration tests."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def server_module(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(REPO_ROOT / "scripts")
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", "test-rag-key")
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_DIM", "8")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")

    for mod in list(sys.modules):
        if mod.startswith("scripts.task_rag_server") or mod == "task_rag_server":
            sys.modules.pop(mod, None)
    return importlib.import_module("scripts.task_rag_server")


async def _fake_rerank(_query, _docs, top_n=None, **_kwargs):
    assert top_n == 2
    return [
        {"index": 2, "relevance_score": 0.91},
        {"index": 0, "relevance_score": 0.42},
    ]


def test_apply_search_rerank_orders_by_relevance_and_keeps_vector_score(server_module):
    hits = [
        server_module.SearchHit(text="alpha", score=0.10, chunkId="a"),
        server_module.SearchHit(text="beta", score=0.20, chunkId="b"),
        server_module.SearchHit(text="gamma", score=0.30, chunkId="c"),
    ]

    ranked = server_module._apply_search_rerank("query", hits, _fake_rerank, topk=2)

    assert [hit.chunkId for hit in ranked] == ["c", "a"]
    assert [hit.rerankScore for hit in ranked] == [0.91, 0.42]
    assert [hit.vectorScore for hit in ranked] == [0.30, 0.10]
    assert [hit.score for hit in ranked] == [0.30, 0.10]


def test_search_endpoint_reranks_candidates_and_expands_pool(server_module, monkeypatch):
    from scripts.task_rag_server_models import SearchReq

    calls: list[dict[str, object]] = []

    def fake_resolve_targets(_req):
        return ["main"], False

    def fake_resolve_rerank(_req, targets):
        assert targets == ["main"]
        return True, _fake_rerank, "fake-reranker", 30

    def fake_run_single_search(query, topk, container, timeout_s, embedding_override=None):
        calls.append(
            {
                "query": query,
                "topk": topk,
                "container": container,
                "timeout_s": timeout_s,
                "embedding_override": embedding_override,
            }
        )
        payload = {
            "code": "ok",
            "results": [
                {"text": "alpha", "score": 0.10, "taskId": "t1", "chunkId": "a"},
                {"text": "beta", "score": 0.20, "taskId": "t2", "chunkId": "b"},
                {"text": "gamma", "score": 0.30, "taskId": "t3", "chunkId": "c"},
            ],
        }
        return server_module.CommandResponse(command=["fake"], code=0), payload

    monkeypatch.setattr(server_module, "_resolve_search_targets", fake_resolve_targets)
    monkeypatch.setattr(server_module, "_resolve_search_rerank", fake_resolve_rerank)
    monkeypatch.setattr(server_module, "_run_single_search", fake_run_single_search)

    body = server_module.search(SearchReq(query="q", container="main", topk=2))

    assert calls[0]["topk"] == 30
    assert body.rerank_applied is True
    assert body.reranker == "fake-reranker"
    assert [hit.chunkId for hit in body.results] == ["c", "a"]
    assert [hit.rerankScore for hit in body.results] == [0.91, 0.42]
    assert [hit.vectorScore for hit in body.results] == [0.30, 0.10]
