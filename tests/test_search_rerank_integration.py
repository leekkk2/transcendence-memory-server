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


@pytest.mark.parametrize('bad', [
    {'index': 0, 'relevance_score': float('nan')},
    {'index': 1, 'relevance_score': float('inf')},
    {'index': 9, 'relevance_score': .4},
    {'index': 0, 'relevance_score': .5},
    {'index': .5, 'relevance_score': .5},
    {}, None,
])
def test_invalid_rerank_is_atomic(server_module, bad):
    hits = [server_module.SearchHit(text='a', score=0), server_module.SearchHit(text='b', score=1)]
    async def rerank(*args, **kwargs):
        return [{'index': 0, 'relevance_score': .2}, bad]
    with pytest.raises(ValueError):
        server_module._apply_search_rerank('q', hits, rerank, 2)
    assert all(h.rerankScore is None for h in hits)


def test_rerank_includes_title_and_sorts_before_topk(server_module):
    hits = [
        server_module.SearchHit(title='精确标题', text='long body', score=0.9),
        server_module.SearchHit(text='other body', score=0.1),
        server_module.SearchHit(source='source only', score=0.2),
    ]

    async def rerank(query, docs, top_n):
        assert docs == ['精确标题\n\nlong body', 'other body', 'source only']
        assert top_n == 2
        return [
            {'index': 1, 'relevance_score': 0.1},
            {'index': 0, 'relevance_score': 0.9},
            {'index': 2, 'relevance_score': 0.5},
        ]

    ranked = server_module._apply_search_rerank('精确标题', hits, rerank, 2)

    assert [h.rerankScore for h in ranked] == [0.9, 0.5]
    assert [h.score for h in ranked] == [0.9, 0.2]


def test_search_recalls_title_outside_dense_pool_and_reranks_it(server_module, tmp_path, monkeypatch):
    import lancedb
    import pyarrow as pa

    query = 'aws-eva 磁盘清理转存网盘与文件索引'
    title = query + ' @ 2026-09-11 实战'
    data = pa.table({
        'taskId': [f'noise-{i}' for i in range(35)] + ['target'],
        'chunkId': [f'chunk-{i}' for i in range(36)],
        'title': ['历史运维'] * 35 + [title],
        'text': ['ordinary body'] * 35 + ['long historical operational note'],
        'vector': pa.array([[0.1, 0.0]] * 35 + [[3.0, 4.0]], type=pa.list_(pa.float32(), 2)),
    })
    db_path = tmp_path / 'db'
    lancedb.connect(str(db_path)).create_table('chunks', data)
    runtime = importlib.import_module('task_rag_runtime')
    monkeypatch.setattr(runtime, 'lancedb_dir', lambda _: db_path)
    monkeypatch.setattr(runtime, 'embed_text', lambda text, mode: [0., 0.])
    monkeypatch.setattr(server_module, '_resolve_search_targets', lambda _: (['main'], False))

    async def rerank(_query, docs, top_n):
        assert len(docs) == 31  # the title match survived outside dense top 30
        assert any(doc.startswith(title + '\n\n') for doc in docs)
        return [{'index': i, 'relevance_score': 0.99 if doc.startswith(title) else 0.1}
                for i, doc in enumerate(docs)]

    monkeypatch.setattr(server_module, '_resolve_search_rerank', lambda *_: (True, rerank, 'test-reranker', 30))

    body = server_module.search(server_module.SearchReq(query=query, container='main', topk=20, rerank=True))

    assert body.status == 'ok'
    assert body.rerank_applied is True
    assert len(body.results) == 20
    assert body.results[0].taskId == 'target'
    assert body.results[0].rerankScore == 0.99
    assert body.results[0].vectorScore == pytest.approx(25.0)
    assert [h.rerankScore for h in body.results] == sorted([h.rerankScore for h in body.results], reverse=True)
