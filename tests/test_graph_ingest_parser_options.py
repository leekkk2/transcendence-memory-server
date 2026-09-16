from unittest.mock import AsyncMock
from pathlib import Path
import asyncio
from scripts import task_rag_graph_ingest as worker

def test_cpu_parser_backend_is_explicit(monkeypatch,tmp_path):
    rag=type('Rag',(),{})()
    rag.process_document_complete=AsyncMock()
    rag.lightrag=None
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, 'raganything_engine', SimpleNamespace(get_raganything=AsyncMock(return_value=rag)))
    monkeypatch.setenv('RAG_PARSER_BACKEND','pipeline')
    p=tmp_path/'sample.pdf';p.write_bytes(b'%PDF test')
    asyncio.run(worker._ingest_file('fixture',p,None))
    assert rag.process_document_complete.call_args.kwargs['backend']=='pipeline'
