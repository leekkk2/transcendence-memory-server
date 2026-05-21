"""增量 embed + 断点续传定向重试 + 失败落 backlog 的回归测试。

覆盖：
- 增量：同容器第二次 rebuild_rows 对未变对象 0 次 embed 调用。
- 迁移前老表（无 content_hash 列）安全全量重嵌。
- 429 失败 chunk 进 backlog 且 error_class=='quota'，rebuild_rows 不 raise。
- 断点续传：已 resolved 的 chunk 不再被 claim / 不再 embed。
- TM_INCREMENTAL_EMBED=0 回退旧全量行为。
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


# ---- 加载 / 构造辅助 -------------------------------------------------------

def _load_ingest(workspace: Path, monkeypatch, env: dict[str, str] | None = None):
    """以指定 WORKSPACE 重新加载 task_rag_lancedb_ingest 及其依赖。"""
    monkeypatch.setenv("WORKSPACE", str(workspace))
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    # 只重载捕获了 WORKSPACE 的模块。embedding_errors / embed_backlog / index_state
    # 不依赖 workspace —— 若一并 pop 会产生重复类对象，污染同进程其它测试文件的
    # 跨副本 isinstance（典型症状：test_gemini_native_embed 在本文件之后失败）。
    for name in ("task_rag_runtime", "task_rag_lancedb_ingest"):
        sys.modules.pop(name, None)
        sys.modules.pop(f"scripts.{name}", None)
    return importlib.import_module("task_rag_lancedb_ingest")


def _row(chunk_id: str, text: str, container: str) -> dict:
    return {
        "chunkId": chunk_id,
        "taskId": chunk_id.split("#")[0],
        "docType": "client_ingest",
        "sourcePath": "memory_objects.jsonl",
        "section": "client_ingest",
        "text": text,
        "container": container,
        "title": "",
        "source": "",
        "tags": [],
        "metadata": {},
    }


def _make_fake_embed(counter: dict):
    """计数版 embed_text 桩：固定返回 3 维向量，记录调用次数。"""
    def fake(text, mode=None, title=None):  # noqa: ARG001
        counter["n"] += 1
        return np.array([0.1, 0.2, 0.3], dtype="float32")
    return fake


def _write_memory_objects(m, container: str, n: int) -> list[str]:
    """写 memory_objects.jsonl，返回对应的 chunkId 列表。"""
    path = m.memory_objects_path(container)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines, chunk_ids = [], []
    for i in range(n):
        oid = f"mem-{i:03d}"
        lines.append(json.dumps({"id": oid, "text": f"memory text {i}", "title": f"t{i}"}))
        # collect_memory_objects 的 chunkId 格式：{id}#client-ingest#{1-based line}
        chunk_ids.append(f"{oid}#client-ingest#{i + 1}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return chunk_ids


def _patch_common(m, monkeypatch, counter: dict):
    """统一桩：profile dim=3、no-op sleep、计数版 embed_text。"""
    monkeypatch.setattr(m, "_resolve_embedding_meta", lambda c: ("test-model", 3, "test-profile"))
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(m, "embed_text", _make_fake_embed(counter))


# ---- ① 增量：未变对象第二次 0 次 embed ------------------------------------

def test_second_rebuild_skips_unchanged_objects(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"text-{i}", container) for i in range(8)]

    first = m.rebuild_rows(container, rows)
    assert first["ingested"] == 8
    assert counter["n"] == 8, "首次 rebuild 应嵌入全部 8 条"

    counter["n"] = 0
    second = m.rebuild_rows(container, rows)
    assert counter["n"] == 0, f"未变对象第二次 rebuild 不应调用 embed: {second}"
    assert second["skipped_unchanged"] == 8
    assert second["ingested"] == 0
    assert second["total"] == 8


def test_incremental_reembeds_only_changed_object(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"text-{i}", container) for i in range(6)]
    m.rebuild_rows(container, rows)

    # 仅改一条对象的文本 → content_hash 变化 → 只有它重嵌
    counter["n"] = 0
    rows[2]["text"] = "text-2-edited"
    summary = m.rebuild_rows(container, rows)
    assert counter["n"] == 1, f"仅改动 1 条，应只重嵌 1 次: {summary}"
    assert summary["skipped_unchanged"] == 5
    assert summary["ingested"] == 1


def test_incremental_disabled_falls_back_to_full_reembed(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch, env={"TM_INCREMENTAL_EMBED": "0"})
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"text-{i}", container) for i in range(5)]
    m.rebuild_rows(container, rows)

    # 关闭增量 → 第二次仍全量重嵌
    counter["n"] = 0
    summary = m.rebuild_rows(container, rows)
    assert counter["n"] == 5, f"TM_INCREMENTAL_EMBED=0 应回退全量重嵌: {summary}"
    assert summary["skipped_unchanged"] == 0


# ---- ② 迁移前老表（无 content_hash 列）安全全量重嵌 ------------------------

def test_legacy_table_without_content_hash_full_reembed(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"text-{i}", container) for i in range(5)]

    # 手工建一张迁移前老表：metadata 已是 string，但缺 content_hash / embedded_at 列。
    old_rows = []
    for r in rows:
        old_rows.append({
            "chunkId": r["chunkId"], "taskId": r["taskId"], "docType": r["docType"],
            "sourcePath": r["sourcePath"], "section": r["section"], "text": r["text"],
            "container": container, "title": "", "source": "", "tags": [],
            "metadata": "{}", "embedding_model": "", "embedding_dim": 0,
            "embedding_profile": "", "vector": [0.1, 0.2, 0.3],
        })
    db = lancedb.connect(str(m.lancedb_dir(container)))
    db.create_table("chunks", data=old_rows)
    assert not m._chunk_schema_has_content_hash(db.open_table("chunks"))

    summary = m.rebuild_rows(container, rows)
    assert counter["n"] == 5, f"老表无 content_hash 列应安全全量重嵌: {summary}"
    assert summary["skipped_unchanged"] == 0
    # 重嵌后表已含 content_hash 列 → 下次可走增量
    assert m._chunk_schema_has_content_hash(
        lancedb.connect(str(m.lancedb_dir(container))).open_table("chunks")
    )


# ---- ③ 删孤儿不误删：embed_specific_chunks 永不触碰删孤儿 -------------------

def test_embed_specific_chunks_never_deletes_orphans(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    chunk_ids = _write_memory_objects(m, container, 100)

    # 先全量嵌入 100 个 chunk
    rows = m._collect_fresh_rows(container)
    first = m.rebuild_rows(container, rows)
    assert first["total"] == 100

    # 定向重试其中 2 个 —— 其余 98 个必须原封不动
    counter["n"] = 0
    summary = m.embed_specific_chunks(container, [chunk_ids[10], chunk_ids[20]])
    assert summary["embedded"] == 2
    assert counter["n"] == 2, "embed_specific_chunks 只应处理给定的 2 个 chunk"

    table = lancedb.connect(str(m.lancedb_dir(container))).open_table("chunks")
    assert int(table.count_rows()) == 100, "98 个未处理 chunk 不得被删孤儿误删"
    present = {str(r.get("chunkId")) for r in table.to_arrow().to_pylist()}
    assert present == set(chunk_ids)


# ---- ④ 429 失败 chunk 进 backlog 且 error_class=='quota' --------------------

def test_quota_failure_records_backlog_and_exits_clean(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    monkeypatch.setattr(m, "_resolve_embedding_meta", lambda c: ("test-model", 3, "test-profile"))
    monkeypatch.setattr(m.time, "sleep", lambda *a, **k: None)

    def fail_429(text, mode=None, title=None):  # noqa: ARG001
        # legacy 裸 RuntimeError 形态：消息塞 429 → classify 归类 quota
        raise RuntimeError("embedding upstream 429: RESOURCE_EXHAUSTED")

    monkeypatch.setattr(m, "embed_text", fail_429)
    container = "testbox"
    rows = [_row(f"obj{i}#client-ingest#{i}", f"t{i}", container) for i in range(3)]

    # 静默成功语义：失败全部落 backlog → 不 raise、返回正常 summary
    summary = m.rebuild_rows(container, rows)
    assert summary["ingested"] == 0
    assert summary["backlog"] == 3, summary

    store = m._get_backlog_store()
    items = store.list_items(container)
    assert len(items) == 3
    assert all(it.error_class == "quota" for it in items)
    assert all(it.status == "waiting" for it in items)


# ---- ⑤ 断点续传：已 resolved 的 chunk 不再被 claim / 不再 embed --------------

def test_retry_does_not_reembed_already_resolved(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    chunk_ids = _write_memory_objects(m, container, 5)

    store = m._get_backlog_store()
    for cid in chunk_ids:
        store.record_failure(container, cid, "quota", content_hash="stale")
    # 模拟前一轮 retry 已成功解决其中 2 个
    store.mark_resolved_many(container, chunk_ids[:2])

    # claim 用未来时刻让退避到期；resolved 行不会被 claim
    future = 9_999_999_999
    due = store.claim_due(container, 50, now=future)
    assert sorted(it.chunk_id for it in due) == sorted(chunk_ids[2:])

    summary = m.embed_specific_chunks(
        container, [it.chunk_id for it in due], backlog_store=store,
    )
    assert counter["n"] == 3, f"只应重嵌 3 个 waiting chunk: {summary}"
    assert summary["embedded"] == 3

    # 重试成功后该批立即 mark_resolved → 再无可 claim 的行
    assert store.claim_due(container, 50, now=future) == []
    counts = store.counts(container)
    assert counts["resolved"] == 5
    assert counts["waiting"] == 0 and counts["retrying"] == 0


def test_embed_backlog_retry_resolves_when_source_object_deleted(tmp_path, monkeypatch):
    """源对象已删除的 backlog chunk → mark_resolved，防止无限堆积。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    m = _load_ingest(ws, monkeypatch)
    counter = {"n": 0}
    _patch_common(m, monkeypatch, counter)
    container = "testbox"
    _write_memory_objects(m, container, 2)  # 源里只有 2 个对象

    store = m._get_backlog_store()
    store.record_failure(container, "ghost#client-ingest#999", "quota", content_hash="x")
    summary = m.embed_specific_chunks(
        container, ["ghost#client-ingest#999"], backlog_store=store,
    )
    assert summary["missing"] == 1
    assert counter["n"] == 0
    assert store.counts(container)["resolved"] == 1
