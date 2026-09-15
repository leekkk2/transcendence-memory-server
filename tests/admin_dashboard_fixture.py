"""Isolated dashboard contract fixture; no models or production credentials."""
import os
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT / ".local" / "admin-e2e"
os.environ.update(WORKSPACE=str(WS), RAG_API_KEY="tm-local-browser-fixture", TM_DISABLE_WORKER="1", TM_ENV="dev",
                  EMBEDDING_API_KEY="fixture", LLM_API_KEY="fixture")
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import task_rag_server as server

path = WS / "tasks/rag/containers/admin-ui-test/raganything"
path.mkdir(parents=True, exist_ok=True)
(path / "kv_store_doc_status.json").write_text(json.dumps({
    "doc-fixture": {"status": "processed", "file_path": "manual.pdf", "content_summary": "The Aurora telescope is maintained by Atlas Lab.", "chunks_count": 1, "content_length": 55},
    "doc-failed": {"status": "failed", "error_msg": "Parser rejected the file", "file_path": "broken.pdf"},
}))
(path / "kv_store_full_docs.json").write_text(json.dumps({"doc-fixture": {"content": "The Aurora telescope is maintained by Atlas Lab."}}))
(path / "kv_store_text_chunks.json").write_text(json.dumps({"chunk-a": {"full_doc_id": "doc-fixture", "content": "Atlas Lab maintains Aurora.", "chunk_order_index": 0}}))
(path / "graph_chunk_entity_relation.graphml").write_text('''<graphml xmlns="http://graphml.graphdrawing.org/xmlns"><key id="d0" for="node" attr.name="entity_type"/><key id="d1" for="all" attr.name="description"/><graph edgedefault="undirected"><node id="Aurora"><data key="d0">telescope</data><data key="d1">An optical telescope</data></node><node id="Atlas Lab"><data key="d0">organization</data></node><edge source="Aurora" target="Atlas Lab"><data key="d1">maintained by</data></edge></graph></graphml>''')
server.resolve_container_or_raise = lambda name: (name, None)
server._require_lightrag_ready = lambda: None
server.get_raganything = object()

async def fixture_health(container=None):
    ready = dict.fromkeys(["search", "embed", "ingest_objects", "ingest_structured", "query", "documents_text", "documents_file"], True)
    return {"status": "ok", "service": "transcendence-memory-server", "architecture": "rag-anything",
            "build_flavor": "full", "multimodal_capable": True, "degraded_reasons": [], "runtime_ready": ready,
            "accepting_ingest": True, "worker_running": True, "uptime_seconds": 600,
            "system_status": {"memory": "ok"}, "public_warnings": []}
server._collect_health_state = fixture_health

import uvicorn
uvicorn.run(server.app, host="127.0.0.1", port=18712, log_level="warning")
