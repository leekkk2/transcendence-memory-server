"""Graph ingestion must close owned async workers before asyncio.run exits."""
import asyncio
import importlib
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("mode", ["text", "file"])
@pytest.mark.parametrize("fail", [False, True])
def test_ingestion_shuts_down_all_model_workers(tmp_path, monkeypatch, mode, fail):
    mod = importlib.import_module("scripts.task_rag_graph_ingest")
    events = []

    class Model:
        def __init__(self, name):
            self.name = name

        async def shutdown(self):
            events.append(self.name)

    class Rag:
        llm_model_func = Model("llm")
        embedding_func = SimpleNamespace(func=Model("embedding"))
        rerank_model_func = Model("rerank")

        async def ainsert(self, text):
            events.append("insert")
            if fail:
                raise ValueError("upstream rejected")

        async def finalize_storages(self):
            events.append("storage")

    rag = Rag()

    async def get_rag(_container):
        return rag

    async def process(**kwargs):
        await rag.ainsert("document")

    async def get_any(_container):
        return SimpleNamespace(lightrag=rag, process_document_complete=process)

    monkeypatch.setattr(mod, "get_lightrag", get_rag)
    monkeypatch.setitem(sys.modules, "raganything_engine", SimpleNamespace(get_raganything=get_any))
    source = tmp_path / "test.txt"
    source.write_text("Aster laboratory maintains the Cedar sensor.")
    call = mod._ingest_text("test", source) if mode == "text" else mod._ingest_file("test", source, None)
    if fail:
        with pytest.raises(ValueError, match="upstream rejected"):
            asyncio.run(call)
    else:
        asyncio.run(call)
    assert events == ["insert", "llm", "embedding", "rerank", "storage"]


def test_ingestion_cleanup_closes_remaining_resources_on_error(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.task_rag_graph_ingest")
    events = []

    class Model:
        async def shutdown(self):
            events.append("llm")
            raise RuntimeError("shutdown failed")

    class Rag:
        llm_model_func = Model()
        embedding_func = None
        rerank_model_func = None

        async def ainsert(self, text):
            raise ValueError("original ingest failure")

        async def finalize_storages(self):
            events.append("storage")

    async def get_rag(_container):
        return Rag()

    monkeypatch.setattr(mod, "get_lightrag", get_rag)
    source = tmp_path / "test.txt"
    source.write_text("Cedar sensor")
    with pytest.raises(ValueError, match="original ingest failure"):
        asyncio.run(mod._ingest_text("test", source))
    assert events == ["llm", "storage"]


def test_ingestion_closes_role_queues_on_current_lightrag(tmp_path, monkeypatch):
    mod = importlib.import_module("scripts.task_rag_graph_ingest")
    events = []

    class Role:
        async def shutdown(self):
            events.append("role")

    class Rag:
        role_llm_funcs = {"extract": Role(), "summary": Role()}
        llm_model_func = None
        embedding_func = None
        rerank_model_func = None

        async def ainsert(self, text):
            pass

        async def finalize_storages(self):
            events.append("storage")

    async def get_rag(_container):
        return Rag()

    monkeypatch.setattr(mod, "get_lightrag", get_rag)
    source = tmp_path / "test.txt"
    source.write_text("Cedar sensor")
    asyncio.run(mod._ingest_text("test", source))
    assert events == ["role", "role", "storage"]
