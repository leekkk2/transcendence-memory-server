"""Blueprint P4 单测：citation 行号 + LLM 答案溯源 + fallback_template。

纯逻辑，不依赖 lancedb / fastapi / 外网 —— 只 import scripts/rag_citation.py
（无重依赖）。覆盖交付清单 §5 四类：

  (a) chunk 行号提取：text → chunks 带正确 1-based lineStart/lineEnd。
  (b) [Chunk_ID] / References 正则提取 + 映射：样例 answer + chunk map → citations，
      无 marker → 空。
  (c) fallback_template 渲染：配模板 → 结构化体；未配（None/空）→ None。
  (d) 向后兼容：chunk metadata 无行号 → 提取出的 citation lineStart/lineEnd 为 None。

需 lancedb 的端到端 ingest→search 不在此（标 @pytest.mark.integration，见
test_search_union_routing 等已有套件）。
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from rag_citation import (  # noqa: E402
    chunk_lines_with_ranges,
    extract_answer_citations,
    render_fallback_template,
)


# ── (a) chunk 行号提取 ───────────────────────────────────────────────────────


def test_chunk_lines_with_ranges_single_window():
    text = "\n".join(f"line{i}" for i in range(1, 6))  # 5 lines
    out = chunk_lines_with_ranges(text, size=60, overlap=10)
    assert len(out) == 1
    chunk, start, end = out[0]
    assert start == 1
    assert end == 5
    assert chunk == text  # full doc, stripped no-op


def test_chunk_lines_with_ranges_sliding_windows():
    # 10 lines, size=4 overlap=1 → step=3 → windows at idx 0,3,6,9
    text = "\n".join(f"L{i}" for i in range(1, 11))
    out = chunk_lines_with_ranges(text, size=4, overlap=1)
    spans = [(s, e) for _t, s, e in out]
    # 1-based inclusive: [1..4], [4..7], [7..10], [10..10]
    assert spans == [(1, 4), (4, 7), (7, 10), (10, 10)]
    # last window covers only line 10
    assert out[-1][0].strip() == "L10"


def test_chunk_lines_with_ranges_empty():
    assert chunk_lines_with_ranges("") == []
    assert chunk_lines_with_ranges("   \n   ") == []  # whitespace-only windows dropped


def test_chunk_lines_text_view_matches_legacy():
    # The text-only projection must equal the legacy chunk_lines behavior:
    # '\n'.join(window).strip(), empty windows dropped.
    text = "\n".join(f"row{i}" for i in range(1, 8))
    texts = [t for t, _s, _e in chunk_lines_with_ranges(text, size=3, overlap=1)]

    def _legacy(t, size=3, overlap=1):
        lines = t.splitlines()
        step = max(1, size - overlap)
        return [
            c
            for c in ("\n".join(lines[i:i + size]).strip() for i in range(0, len(lines), step))
            if c
        ]

    assert texts == _legacy(text)


# ── (b) [Chunk_ID] / References 提取 + 映射 ──────────────────────────────────


def _chunk(cid, path, line_start=None, line_end=None, title="", section="memory", score=0.1):
    meta = {}
    if line_start is not None:
        meta["lineStart"] = line_start
    if line_end is not None:
        meta["lineEnd"] = line_end
    return {
        "chunkId": cid,
        "sourcePath": path,
        "section": section,
        "title": title,
        "score": score,
        "container": "box",
        "metadata": meta,
    }


def test_extract_literal_chunk_id_markers():
    chunk_map = [
        _chunk("doc-a#0", "/m/doc-a.md", 1, 60),
        _chunk("doc-b#2", "/m/doc-b.md", 120, 180),
    ]
    answer = "The plan is X [doc-a#0] and the rollback is Y [doc-b#2]."
    cites = extract_answer_citations(answer, chunk_map)
    assert [c["chunkId"] for c in cites] == ["doc-a#0", "doc-b#2"]
    assert cites[0]["lineStart"] == 1 and cites[0]["lineEnd"] == 60
    assert cites[1]["lineStart"] == 120 and cites[1]["lineEnd"] == 180


def test_extract_lightrag_references_section_by_title():
    chunk_map = [
        _chunk("doc-a#0", "/m/design.md", 1, 60, title="Design Notes"),
        _chunk("doc-b#1", "/m/runbook.md", 5, 40, title="Runbook"),
    ]
    answer = (
        "Answer body referencing [1] and [2].\n\n"
        "### References\n"
        "- [1] Design Notes\n"
        "- [2] Runbook\n"
    )
    cites = extract_answer_citations(answer, chunk_map)
    paths = [c["sourcePath"] for c in cites]
    assert "/m/design.md" in paths
    assert "/m/runbook.md" in paths


def test_extract_lightrag_references_by_path_basename():
    # Title in references reads like a filename → matched on sourcePath basename.
    chunk_map = [_chunk("doc-x#0", "/store/handbook.md", 3, 9, title="")]
    answer = "Body.\n\n### References\n- [1] handbook.md\n"
    cites = extract_answer_citations(answer, chunk_map)
    assert len(cites) == 1
    assert cites[0]["sourcePath"] == "/store/handbook.md"
    assert cites[0]["lineStart"] == 3 and cites[0]["lineEnd"] == 9


def test_extract_no_markers_returns_empty():
    chunk_map = [_chunk("doc-a#0", "/m/doc-a.md", 1, 60)]
    assert extract_answer_citations("Plain answer, no brackets at all.", chunk_map) == []


def test_extract_unresolvable_marker_returns_empty():
    # Marker present but maps to nothing in the chunk set → empty, never raises.
    chunk_map = [_chunk("doc-a#0", "/m/doc-a.md", 1, 60)]
    answer = "See [nonexistent#9] for details."
    assert extract_answer_citations(answer, chunk_map) == []


def test_extract_pure_integer_not_treated_as_chunk_id():
    # Bare [1] outside a references heading shouldn't false-match a chunkId.
    chunk_map = [_chunk("1", "/m/weird.md", 1, 5)]  # pathological chunkId "1"
    answer = "Step [1] do the thing."  # no references section
    # _looks_like_chunk_id rejects pure digits → no literal match; no refs section.
    assert extract_answer_citations(answer, chunk_map) == []


def test_extract_empty_inputs():
    assert extract_answer_citations("", [{"chunkId": "x#0"}]) == []
    assert extract_answer_citations("answer [x#0]", []) == []
    assert extract_answer_citations(None, None) == []


def test_extract_references_line_without_heading_not_cited():
    # A ref-shaped line "- [1] Some Title" sitting in the prose (no "### References"
    # heading) must NOT produce a dialect-B citation — the heading is the only anchor.
    chunk_map = [_chunk("doc-a#0", "/m/design.md", 1, 60, title="Design Notes")]
    answer = "Background paragraph.\n- [1] Design Notes\nMore prose, no references heading."
    assert extract_answer_citations(answer, chunk_map) == []


def test_extract_references_only_after_heading():
    # Positive/negative contrast on the SAME chunk map: the bare ref line before the
    # heading is ignored; only the entry under "### References" is cited.
    chunk_map = [
        _chunk("doc-a#0", "/m/design.md", 1, 60, title="Design Notes"),
        _chunk("doc-b#1", "/m/runbook.md", 5, 40, title="Runbook"),
    ]
    answer = (
        "Prose mentioning [1] Design Notes inline as if a list item.\n"
        "- [1] Design Notes\n"  # decoy: before heading → must be ignored
        "\n### References\n"
        "- [2] Runbook\n"
    )
    cites = extract_answer_citations(answer, chunk_map)
    paths = [c["sourcePath"] for c in cites]
    assert paths == ["/m/runbook.md"]  # only the post-heading entry resolves


# ── (c) fallback_template 渲染 ──────────────────────────────────────────────


def test_render_fallback_unconfigured_returns_none():
    assert render_fallback_template(None) is None
    assert render_fallback_template("") is None
    assert render_fallback_template("   \n  ") is None
    assert render_fallback_template(123) is None  # non-str defensive


def test_render_fallback_with_placeholders():
    tpl = "No confident answer for '{query}' (container={container}, threshold={threshold})."
    out = render_fallback_template(
        tpl, {"query": "how to deploy", "container": "test-container", "threshold": 0.35}
    )
    assert out == "No confident answer for 'how to deploy' (container=test-container, threshold=0.35)."


def test_render_fallback_missing_placeholder_left_intact():
    # Unknown token must not raise KeyError; left as literal {token}.
    out = render_fallback_template("Q={query} U={unknown}", {"query": "x"})
    assert out == "Q=x U={unknown}"


def test_render_fallback_no_placeholders_passthrough():
    out = render_fallback_template("Static interception body.", {"query": "ignored"})
    assert out == "Static interception body."


# ── (d) 向后兼容：metadata 无行号 → citation lineStart/lineEnd 为 None ─────────


def test_backcompat_chunk_without_line_metadata():
    # Legacy chunk ingested before P4: metadata has no lineStart/lineEnd.
    legacy = _chunk("legacy#0", "/m/old.md")  # no line_start/line_end → empty meta
    assert legacy["metadata"] == {}
    cites = extract_answer_citations("ref [legacy#0]", [legacy])
    assert len(cites) == 1
    assert cites[0]["lineStart"] is None
    assert cites[0]["lineEnd"] is None
    assert cites[0]["chunkId"] == "legacy#0"


def test_backcompat_metadata_missing_key_entirely():
    # metadata dict present but missing the keys (mixed old/new corpus).
    c = {
        "chunkId": "mix#1",
        "sourcePath": "/m/mix.md",
        "section": "memory",
        "title": "",
        "score": 0.2,
        "container": "box",
        "metadata": {"project": "demo"},  # unrelated keys only
    }
    cites = extract_answer_citations("see [mix#1]", [c])
    assert cites[0]["lineStart"] is None and cites[0]["lineEnd"] is None
