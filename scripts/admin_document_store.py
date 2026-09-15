"""Bounded, read-only views of persisted LightRAG document and graph snapshots."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

try:
    from secret_redaction import redact_text
except ImportError:
    from scripts.secret_redaction import redact_text

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_GRAPH_BYTES = 128 * 1024 * 1024
STATES = {"pending", "parsing", "analyzing", "processing", "handling", "processed", "failed"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}


class SnapshotUnavailable(Exception):
    """Missing/partial snapshots must not be reported as successful ingestion."""


def _file(root: Path, name: str, maximum: int) -> Path | None:
    path = root / name
    if path.is_symlink() or root.is_symlink():
        raise SnapshotUnavailable("Snapshot path is not a regular file.")
    if not path.exists():
        return None
    if not path.is_file() or path.resolve().parent != root.resolve():
        raise SnapshotUnavailable("Snapshot path is not a regular file.")
    if path.stat().st_size > maximum:
        raise SnapshotUnavailable("Snapshot exceeds the admin reader size limit.")
    return path


def _json(root: Path, name: str) -> dict:
    path = _file(root, name, MAX_JSON_BYTES)
    if path is None:
        return {}
    try:
        with path.open("rb") as stream:
            data = json.loads(stream.read(MAX_JSON_BYTES + 1))
        if not isinstance(data, dict):
            raise ValueError("object required")
        return data
    except (OSError, ValueError) as exc:
        raise SnapshotUnavailable("Snapshot is being updated or is unreadable; refresh shortly.") from exc


def _safe(value, limit: int = 1000) -> str:
    return redact_text(str(value or ""))[:limit]


def _number(value) -> int:
    try:
        return max(0, int(value or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _source(value) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1]
    return _safe(re.sub(r"^file-[0-9a-f]{32}-", "", name), 255)


def _document(doc_id: str, row: dict) -> dict:
    source = _source(row.get("file_path"))
    suffix = Path(source).suffix.lower()
    kind = "pdf" if suffix == ".pdf" else "image" if suffix in IMAGE_SUFFIXES else "text" if suffix in {"", ".txt", ".md"} else "file"
    state = str(row.get("status") or "unknown").lower()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return {
        "id": doc_id, "status": state if state in STATES else "unknown",
        "source": source, "kind": kind,
        "summary": _safe(row.get("content_summary"), 1200),
        "chars": _number(row.get("content_length")),
        "chunks": _number(row.get("chunks_count")),
        "created_at": _safe(row.get("created_at"), 80),
        "updated_at": _safe(row.get("updated_at"), 80),
        "error": _safe(row.get("error_msg"), 2000),
        "duplicate": doc_id.startswith("dup-") or bool(metadata.get("is_duplicate")),
    }


class DocumentStore:
    def __init__(self, container_dir: Path):
        self.root = container_dir / "raganything"

    def list_documents(self, *, status="", kind="", q="", offset=0, limit=20, include_duplicates=False):
        raw = _json(self.root, "kv_store_doc_status.json")
        rows = [_document(key, row) for key, row in raw.items() if isinstance(row, dict)]
        duplicates = sum(row["duplicate"] for row in rows)
        visible = rows if include_duplicates else [row for row in rows if not row["duplicate"]]
        counts = dict(Counter(row["status"] for row in visible))
        needle = q.casefold()
        filtered = [row for row in visible if (not status or row["status"] == status)
                    and (not kind or row["kind"] == kind)
                    and (not needle or needle in (row["id"] + row["source"] + row["summary"]).casefold())]
        filtered.sort(key=lambda row: (row["updated_at"] or row["created_at"], row["id"]), reverse=True)
        return {"documents": filtered[offset:offset + limit], "total": len(filtered),
                "counts": counts, "duplicate_count": duplicates,
                "storage_state": "available" if raw else "empty", "offset": offset, "limit": limit}

    def document(self, doc_id, *, text_offset=0, text_limit=12000, chunk_offset=0, chunk_limit=10):
        raw = _json(self.root, "kv_store_doc_status.json")
        row = raw.get(doc_id)
        if not isinstance(row, dict):
            return None
        result = _document(doc_id, row)
        full = _json(self.root, "kv_store_full_docs.json").get(doc_id) or {}
        text = _safe(full.get("content") if isinstance(full, dict) else "", MAX_JSON_BYTES)
        chunks = _json(self.root, "kv_store_text_chunks.json")
        matching = [(key, data) for key, data in chunks.items()
                    if isinstance(data, dict) and data.get("full_doc_id") == doc_id]
        matching.sort(key=lambda pair: _number(pair[1].get("chunk_order_index")))
        page = [{"id": key, "content": _safe(data.get("content"), 6000),
                 "order": _number(data.get("chunk_order_index")),
                 "tokens": _number(data.get("tokens"))}
                for key, data in matching[chunk_offset:chunk_offset + chunk_limit]]
        result.update(text=text[text_offset:text_offset + text_limit], text_length=len(text),
                      text_offset=text_offset, text_truncated=text_offset + text_limit < len(text),
                      chunks=page, chunks_total=len(matching), chunk_offset=chunk_offset)
        return result

    def graph(self, *, q="", node_limit=60, edge_limit=120):
        path = _file(self.root, "graph_chunk_entity_relation.graphml", MAX_GRAPH_BYTES)
        if path is None:
            return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0,
                    "entity_types": {}, "sampled": False, "storage_state": "empty"}
        stat = path.stat()
        return _graph(str(path), stat.st_mtime_ns, stat.st_size, q.casefold(), node_limit, edge_limit)


class _XMLReader:
    def __init__(self, stream):
        self.stream = stream
        self.tail = b""

    def read(self, size=-1):
        data = self.stream.read(size)
        probe = (self.tail + data).upper()
        if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
            raise SnapshotUnavailable("Unsupported XML declarations in graph snapshot.")
        self.tail = probe[-16:]
        return data


def _graph_data(element, keys):
    return {keys.get(child.get("key"), ""): child.text or ""
            for child in element if child.tag.rsplit("}", 1)[-1] == "data"}


def _graph_node(element, keys):
    data = _graph_data(element, keys)
    return {"id": element.get("id", ""), "label": _safe(element.get("id"), 160),
            "type": _safe(data.get("entity_type"), 80) or "unknown",
            "description": _safe(data.get("description"), 2000)}


@lru_cache(maxsize=8)
def _graph(filename, mtime, size, needle, node_limit, edge_limit):
    keys, nodes, edges, ids = {}, [], [], set()
    counts = Counter()
    node_count = edge_count = 0
    graph_element = None
    try:
        with open(filename, "rb") as stream:
            for event, elem in ET.iterparse(_XMLReader(stream), events=("start", "end")):
                tag = elem.tag.rsplit("}", 1)[-1]
                if event == "start":
                    if tag == "graph":
                        graph_element = elem
                    continue
                if tag == "key":
                    keys[elem.get("id")] = elem.get("attr.name", "")
                elif tag == "node":
                    node_count += 1
                    node = _graph_node(elem, keys)
                    counts[node["type"]] += 1
                    if len(nodes) < node_limit and (not needle or needle in (node["label"] + node["description"]).casefold()):
                        nodes.append(node)
                        ids.add(node["id"])
                elif tag == "edge":
                    edge_count += 1
                    source, target = elem.get("source"), elem.get("target")
                    if len(edges) < edge_limit and source in ids and target in ids:
                        data = _graph_data(elem, keys)
                        edges.append({"id": f"edge-{edge_count}", "source": source, "target": target,
                                      "description": _safe(data.get("description"), 1200),
                                      "keywords": _safe(data.get("keywords"), 200)})
                if tag in {"node", "edge"} and graph_element is not None:
                    graph_element.remove(elem)
    except (OSError, ET.ParseError) as exc:
        raise SnapshotUnavailable("Graph snapshot is being updated or unreadable; refresh shortly.") from exc
    return {"nodes": nodes, "edges": edges, "node_count": node_count, "edge_count": edge_count,
            "entity_types": dict(counts), "sampled": len(nodes) < node_count or len(edges) < edge_count,
            "storage_state": "available"}
