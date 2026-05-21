"""删孤儿安全性回归测试。

危险边缘：增量 embed 全跳过时 ingested==0，旧守卫 `ingested > 0` 会漏删真正
被删除的对象。守卫改为 `full_rebuild` 后：
- full_rebuild=True（默认 /embed 行为）：即便全跳过也删真正消失的对象。
- full_rebuild=False / embed_specific_chunks：永不删孤儿。
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("lancedb")
import lancedb
import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_ingest(workspace: Path, monkeypatch):
    monkeypatch.setenv("WORKSPACE", str(workspace))
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    # 只重载捕获了 WORKSPACE 的模块（详见 test_incremental_embed._load_ingest 注释）。
    for name in ("task_rag_runtime", "task_rag_lancedb_ingest"):
        sys.modules.pop(name, None)
        sys.modules.pop(f"scripts.{name}", None)
    return importlib.import_module("task_rag_lancedb_ingest")


def _row(chunk_id: str, text: str, container: str) -> dict:
    return {
        "chunkId": chunk_id, "taskId": chunk_id.split("#")[0],
        "docType": "client_ingest", "sourcePath": "memory_objects.jsonl",
        "section": "client_ingest", "text": text, "container": container,
        "title": "", "source": "", "tags": [], "metadata": {},
    }


def _patch_common(m, monkeypatch):
    counter = {"n": 0}

    def fake(text, mode=None, title=None):  # noqa: ARG001
        counter["n"] += 1
        return np.array([0.1, 0.2, 0.3], dtype="float32")

    monkeypatch.setattr(m, "_resolve_embedding_meta", lambda c: ("test-model", 3, "test-profile"))
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(m, "embed_text", fake)
    return counter


def _chunk_ids(m, container: str) -> set[str]:
    table = lancedb.connect(str(m.lancedb_dir(container))).open_table("chunks")
    return {str(r.get("chunkId")) for r in table.to_arrow().to_pylist()}


def test_full_rebuild_deletes_truly_removed_objects(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    _patch_common(m, monkeypatch)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"t{i}", container) for i in range(5)]
    m.rebuild_rows(container, rows)
    assert len(_chunk_ids(m, container)) == 5

    # 删掉 2 个对象后再 rebuild → 删孤儿应同步移除
    summary = m.rebuild_rows(container, rows[:3])
    assert summary["total"] == 3
    assert _chunk_ids(m, container) == {r["chunkId"] for r in rows[:3]}


def test_orphan_delete_runs_even_when_all_chunks_skipped(tmp_path, monkeypatch):
    """守卫修复核心场景：增量全跳过 ingested==0，仍须删真正消失的对象。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = _patch_common(m, monkeypatch)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"t{i}", container) for i in range(5)]
    m.rebuild_rows(container, rows)

    # 第二次：保留的 4 条文本未变（全部跳过 → ingested==0），但第 5 条已删除
    counter["n"] = 0
    summary = m.rebuild_rows(container, rows[:4])
    assert counter["n"] == 0, "4 条未变对象不应重嵌"
    assert summary["ingested"] == 0
    assert summary["skipped_unchanged"] == 4
    assert summary["total"] == 4, "ingested==0 时仍须删掉真正消失的第 5 条"
    assert _chunk_ids(m, container) == {r["chunkId"] for r in rows[:4]}


def test_partial_rebuild_preserves_orphans(tmp_path, monkeypatch):
    """full_rebuild=False 永不删孤儿 —— 子集重嵌的结构性安全保证。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    _patch_common(m, monkeypatch)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"t{i}", container) for i in range(5)]
    m.rebuild_rows(container, rows)

    # 只传 2 条但 full_rebuild=False → 另外 3 条不得被当作孤儿删除
    summary = m.rebuild_rows(container, rows[:2], full_rebuild=False)
    assert summary["total"] == 5
    assert _chunk_ids(m, container) == {r["chunkId"] for r in rows}


def test_embed_specific_chunks_preserves_all_other_chunks(tmp_path, monkeypatch):
    """embed_specific_chunks 处理子集时其余 chunk 全部存活（无 delete 调用）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    _patch_common(m, monkeypatch)
    container = "testbox"

    path = m.memory_objects_path(container)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, chunk_ids = [], []
    for i in range(30):
        oid = f"mem-{i:03d}"
        lines.append(json.dumps({"id": oid, "text": f"memory {i}", "title": ""}))
        chunk_ids.append(f"{oid}#client-ingest#{i + 1}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    m.rebuild_rows(container, m._collect_fresh_rows(container))
    assert len(_chunk_ids(m, container)) == 30

    m.embed_specific_chunks(container, [chunk_ids[5], chunk_ids[15], chunk_ids[25]])
    assert _chunk_ids(m, container) == set(chunk_ids), "定向重试不得删除任何其它 chunk"
