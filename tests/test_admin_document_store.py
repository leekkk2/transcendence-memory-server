"""Document browser reads persisted truth without loading a RAG engine."""
import json
from pathlib import Path
import pytest

from scripts.admin_document_store import DocumentStore, SnapshotUnavailable


def store(tmp_path):
    path = tmp_path / "raganything"
    path.mkdir()
    return path, DocumentStore(tmp_path)


def write(path, name, rows):
    (path / name).write_text(json.dumps(rows), encoding="utf-8")


def test_inventory_preserves_real_states_and_filters_duplicates(tmp_path):
    path, reader = store(tmp_path)
    write(path, "kv_store_doc_status.json", {
        "doc-a": {"status": "processed", "file_path": "/private/_inbox/file-" + "a" * 32 + "-guide.pdf", "content_length": 87, "chunks_count": 2, "updated_at": "2026-09-15"},
        "doc-b": {"status": "failed", "error_msg": "password=secret upstream rejected", "updated_at": "2026-09-16"},
        "dup-c": {"status": "failed", "metadata": {"is_duplicate": True}},
        "doc-d": {"status": "processing"},
    })
    data = reader.list_documents(limit=1)
    assert data["total"] == 3
    assert data["counts"] == {"processed": 1, "failed": 1, "processing": 1}
    assert data["duplicate_count"] == 1
    assert data["documents"][0]["id"] == "doc-b"
    assert "secret" not in data["documents"][0]["error"]
    pdf = reader.list_documents(q="guide", kind="pdf")["documents"][0]
    assert pdf["source"] == "guide.pdf"
    assert "/private" not in json.dumps(pdf)
    assert reader.list_documents(status="processed")["total"] == 1
    assert reader.list_documents(offset=100)["documents"] == []


def test_detail_returns_parsed_text_and_chunk_page_without_original_path(tmp_path):
    path, reader = store(tmp_path)
    write(path, "kv_store_doc_status.json", {"doc-a": {"status": "processed", "chunks_list": ["chunk-b", "chunk-a"]}})
    write(path, "kv_store_full_docs.json", {"doc-a": {"content": "alpha " * 200}})
    write(path, "kv_store_text_chunks.json", {
        "chunk-a": {"full_doc_id": "doc-a", "content": "second", "chunk_order_index": 1},
        "chunk-b": {"full_doc_id": "doc-a", "content": "first", "chunk_order_index": 0},
        "chunk-other": {"full_doc_id": "doc-other", "content": "private other doc"},
    })
    data = reader.document("doc-a", text_limit=100, chunk_limit=1)
    assert data["text_truncated"] is True
    assert len(data["text"]) == 100
    assert data["chunks_total"] == 2
    assert data["chunks"][0]["content"] == "first"
    assert "private other" not in json.dumps(data)
    assert reader.document("missing") is None


def test_missing_snapshot_is_empty_but_corruption_is_not_success(tmp_path):
    path, reader = store(tmp_path)
    assert reader.list_documents()["storage_state"] == "empty"
    (path / "kv_store_doc_status.json").write_text("{unfinished")
    with pytest.raises(SnapshotUnavailable):
        reader.list_documents()


def test_snapshot_symlink_and_size_limits_fail_closed(tmp_path, monkeypatch):
    path, reader = store(tmp_path)
    elsewhere = tmp_path / "private.json"
    elsewhere.write_text("{}")
    target = path / "kv_store_doc_status.json"
    target.symlink_to(elsewhere)
    with pytest.raises(SnapshotUnavailable):
        reader.list_documents()
    target.unlink()
    target.write_text('{"doc":{}}')
    monkeypatch.setattr("scripts.admin_document_store.MAX_JSON_BYTES", 2)
    with pytest.raises(SnapshotUnavailable):
        reader.list_documents()


def test_graph_streams_counts_and_bounded_connected_sample(tmp_path):
    path, reader = store(tmp_path)
    (path / "graph_chunk_entity_relation.graphml").write_text('''<?xml version="1.0"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
<key id="d0" for="node" attr.name="entity_type" attr.type="string"/>
<key id="d1" for="all" attr.name="description" attr.type="string"/>
<graph edgedefault="undirected">
<node id="Alpha"><data key="d0">organization</data><data key="d1">First entity</data></node>
<node id="Beta"><data key="d0">location</data></node>
<node id="Gamma"/>
<edge source="Alpha" target="Beta"><data key="d1">Based in</data></edge>
<edge source="Beta" target="Gamma"/>
</graph></graphml>''')
    data = reader.graph(node_limit=2, edge_limit=1)
    assert data["node_count"] == 3
    assert data["edge_count"] == 2
    assert data["sampled"] is True
    assert len(data["nodes"]) == 2
    assert data["edges"][0]["description"] == "Based in"
    ids = {n["id"] for n in data["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])
    assert reader.graph(q="Gamma")["nodes"][0]["id"] == "Gamma"


def test_graph_rejects_entity_declarations(tmp_path):
    path, reader = store(tmp_path)
    (path / "graph_chunk_entity_relation.graphml").write_text(
        '<!DOCTYPE graphml [<!ENTITY x "boom">]><graphml>&x;</graphml>')
    with pytest.raises(SnapshotUnavailable):
        reader.graph()
