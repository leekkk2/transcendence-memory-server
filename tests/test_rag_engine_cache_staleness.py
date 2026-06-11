"""D2 回归：get_lightrag 实例缓存的磁盘存储指纹失效。

主进程缓存 LightRAG 实例后，子进程摄取直接改写 working_dir 下的
graphml/vdb/kv json —— 旧实例对新数据盲视（query 永远 [no-context]，
尽管磁盘向量完好）。修复后：缓存命中先比对 os.stat 指纹，不一致即重建。
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.rag_engine as rag_engine

CONTAINER = "stale-test"


def _install_fake_lightrag(monkeypatch, built: list) -> None:
    """LightRAG 真实初始化太重（存储/分词器），换成记录构造次数的假模块。"""
    class FakeLightRAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            built.append(self)

        async def initialize_storages(self):
            pass

    async def initialize_pipeline_status():
        pass

    lightrag_mod = types.ModuleType("lightrag")
    lightrag_mod.LightRAG = FakeLightRAG
    kg_mod = types.ModuleType("lightrag.kg")
    ss_mod = types.ModuleType("lightrag.kg.shared_storage")
    ss_mod.initialize_pipeline_status = initialize_pipeline_status
    lightrag_mod.kg = kg_mod
    kg_mod.shared_storage = ss_mod
    monkeypatch.setitem(sys.modules, "lightrag", lightrag_mod)
    monkeypatch.setitem(sys.modules, "lightrag.kg", kg_mod)
    monkeypatch.setitem(sys.modules, "lightrag.kg.shared_storage", ss_mod)


@pytest.fixture
def engine_env(tmp_path, monkeypatch):
    """隔离 WS + 假 LightRAG + 假 route 解析；返回 (built, working_dir)。"""
    built: list = []
    rag_engine._lightrag_instances.clear()
    rag_engine._lightrag_locks.clear()
    rag_engine._lightrag_fingerprints.clear()
    _install_fake_lightrag(monkeypatch, built)
    monkeypatch.setattr(rag_engine, "WS", tmp_path)

    route = SimpleNamespace(llm=None, llm_fallbacks=[])

    def fake_resolve(container: str):
        return route, object(), "embed:test", None, "", None

    monkeypatch.setattr(rag_engine, "_resolve_route_emb_rrk", fake_resolve)
    working_dir = tmp_path / "tasks" / "rag" / "containers" / CONTAINER / "raganything"
    yield built, working_dir
    rag_engine._lightrag_instances.clear()
    rag_engine._lightrag_locks.clear()
    rag_engine._lightrag_fingerprints.clear()


def _bump_mtime(path: Path) -> None:
    # 显式 +1s 写 mtime — 不依赖文件系统时间戳精度
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


def test_cache_hit_when_storage_unchanged(engine_env):
    built, working_dir = engine_env

    async def run():
        working_dir.mkdir(parents=True)
        (working_dir / "kv_store_full_docs.json").write_text("{}")
        first = await rag_engine.get_lightrag(CONTAINER)
        second = await rag_engine.get_lightrag(CONTAINER)
        assert first is second

    asyncio.run(run())
    assert len(built) == 1


def test_mtime_change_rebuilds_instance(engine_env):
    built, working_dir = engine_env

    async def run():
        working_dir.mkdir(parents=True)
        doc = working_dir / "kv_store_full_docs.json"
        doc.write_text("{}")
        first = await rag_engine.get_lightrag(CONTAINER)
        # 模拟子进程摄取：内容大小不变，只动 mtime
        _bump_mtime(doc)
        second = await rag_engine.get_lightrag(CONTAINER)
        assert second is not first

    asyncio.run(run())
    assert len(built) == 2


def test_new_storage_file_rebuilds_instance(engine_env):
    built, working_dir = engine_env

    async def run():
        first = await rag_engine.get_lightrag(CONTAINER)
        # 摄取产生全新存储文件（vdb / graphml 任一都应触发）
        (working_dir / "vdb_chunks.json").write_text("{}")
        second = await rag_engine.get_lightrag(CONTAINER)
        assert second is not first
        (working_dir / "graph_chunk_entity_relation.graphml").write_text("<g/>")
        third = await rag_engine.get_lightrag(CONTAINER)
        assert third is not second

    asyncio.run(run())
    assert len(built) == 3


def test_llm_response_cache_does_not_invalidate(engine_env):
    built, working_dir = engine_env

    async def run():
        first = await rag_engine.get_lightrag(CONTAINER)
        # query 路径自身会写 LLM cache —— 计入指纹会导致每次查询自我失效
        (working_dir / "kv_store_llm_response_cache.json").write_text("{}")
        second = await rag_engine.get_lightrag(CONTAINER)
        assert second is first

    asyncio.run(run())
    assert len(built) == 1


def test_storage_fingerprint_is_stat_based(tmp_path):
    fp_empty = rag_engine._storage_fingerprint(tmp_path)
    assert fp_empty == ()

    doc = tmp_path / "kv_store_full_docs.json"
    doc.write_text("{}")
    fp1 = rag_engine._storage_fingerprint(tmp_path)
    assert fp1 != fp_empty

    doc.write_text('{"k": 1}')  # size 变化
    fp2 = rag_engine._storage_fingerprint(tmp_path)
    assert fp2 != fp1

    _bump_mtime(doc)  # 同 size，仅 mtime 变化
    fp3 = rag_engine._storage_fingerprint(tmp_path)
    assert fp3 != fp2

    # 排除清单内的文件不影响指纹
    (tmp_path / "kv_store_llm_response_cache.json").write_text("{}")
    assert rag_engine._storage_fingerprint(tmp_path) == fp3
