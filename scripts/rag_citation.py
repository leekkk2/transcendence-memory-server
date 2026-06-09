#!/usr/bin/env python3
"""Blueprint P4 — citation line ranges, LLM answer source-tracing, fallback template.

Pure functions ONLY. This module imports nothing heavy (no lancedb, no fastapi)
so it can be unit-tested in isolation and imported from both the ingest script
(``task_rag_lancedb_ingest``) and the server (``task_rag_server``).

Three independent capabilities, each additive and graceful by design:

1. ``chunk_lines_with_ranges`` — the line-window chunker now also returns the
   1-based source line span (lineStart/lineEnd) each chunk covers. The legacy
   ``chunk_lines`` text-only behavior is preserved verbatim by deriving the text
   list from the same windows (see ``task_rag_lancedb_ingest.chunk_lines``).

2. ``extract_answer_citations`` — best-effort backfill of citations from an LLM
   answer. Supports two marker dialects and NEVER raises / NEVER mutates the
   answer text. No markers, or no map hit → ``[]``.

3. ``render_fallback_template`` — opt-in structured interception body. Unconfigured
   (None / empty) → returns None so callers keep their current behavior byte-for-byte.

LightRAG output reality (investigated 2026-06-09 against the pinned lightrag
``prompt.py`` / ``utils.generate_reference_list_from_chunks``): LightRAG does
**not** emit ``[Chunk_ID]`` markers. Its answers carry inline numeric ``[n]``
markers plus a trailing ``### References`` section whose entries read
``- [n] Document Title``, where ``n`` is a ``reference_id`` correlated to a
*file_path* (our ``sourcePath``), not a chunkId. ``extract_answer_citations``
therefore handles BOTH the literal ``[Chunk_ID]`` form (forward-compatible / for
clients that post-annotate) AND the realistic LightRAG ``[n] Title`` references
form, mapping each back to the retrieved chunk set by chunkId, then sourcePath
basename, then title. When neither dialect resolves, it returns [] (graceful
no-op) — so for vanilla LightRAG this currently degrades to "cite by sourcePath
title if the References titles happen to match a retrieved chunk's title/path,
else empty". This is honestly a partial bridge, not a guaranteed citation for
every answer; see ``answer_citation_design`` in the P4 report.
"""
from __future__ import annotations

import os
import re
from typing import Any

# ── 1. Line-aware chunking ──────────────────────────────────────────────────


def chunk_lines_with_ranges(
    text: str, size: int = 60, overlap: int = 10
) -> list[tuple[str, int, int]]:
    """Sliding-window chunk a document, keeping each chunk's 1-based line span.

    Returns a list of ``(chunk_text, line_start, line_end)``. The text of each
    chunk is identical to the legacy ``chunk_lines`` output (``'\\n'.join(window)
    .strip()``), and empty/whitespace-only windows are dropped exactly as before
    — so ``[t for t, _, _ in chunk_lines_with_ranges(x)] == chunk_lines(x)``.

    line_start / line_end are 1-based inclusive and refer to the ORIGINAL source
    lines the window spans (before the ``.strip()``), so a citation can point a
    reader at ``sourcePath:line_start-line_end``.
    """
    lines = text.splitlines()
    if not lines:
        return []
    step = max(1, size - overlap)
    out: list[tuple[str, int, int]] = []
    for index in range(0, len(lines), step):
        window = lines[index:index + size]
        chunk = '\n'.join(window).strip()
        if chunk:
            # 1-based inclusive span of the window within the source.
            line_start = index + 1
            line_end = index + len(window)
            out.append((chunk, line_start, line_end))
    return out


# ── 2. LLM answer → citations ───────────────────────────────────────────────

# Inline / references markers. Two dialects:
#   - literal [Chunk_ID]: a bracketed token that looks like a chunkId
#     (contains '#', or '-'/'_' alnum). Forward-compat for post-annotated answers.
#   - LightRAG references: lines like "- [1] Document Title" / "[1] Title" under
#     a "### References" heading; the bracketed token is a small integer.
_BRACKET_TOKEN_RE = re.compile(r'\[([^\[\]\n]{1,200})\]')
_REF_LINE_RE = re.compile(r'^\s*[-*]?\s*\[(\d{1,4})\]\s+(.+?)\s*$', re.M)
_REFERENCES_HEADING_RE = re.compile(r'(?im)^\s*#{0,6}\s*References\s*$')


def _basename(path: str | None) -> str:
    if not path:
        return ''
    return os.path.basename(str(path).rstrip('/')).strip()


def _looks_like_chunk_id(token: str) -> bool:
    """Heuristic: a literal chunkId marker (e.g. 'TASK-001#Summary', 'doc_3#0').

    Pure integers ('1', '2') are NOT chunkIds — those are LightRAG reference_ids
    handled by the references path. A chunkId here means it carries a '#' (our
    ingest chunkId convention ``<id>#<section|index>``).
    """
    token = token.strip()
    if not token:
        return False
    if token.isdigit():
        return False
    return '#' in token


def extract_answer_citations(
    answer: str | None,
    chunk_map: list[dict[str, Any]] | None,
    *,
    max_citations: int = 5,
) -> list[dict[str, Any]]:
    """Map citation markers found in ``answer`` to retrieved chunks.

    ``chunk_map`` is the list of retrieved chunk dicts (as produced by the
    in-process LanceDB search: each has chunkId / sourcePath / section / title /
    metadata{lineStart,lineEnd} / score / container). Returns a list of citation
    dicts (chunkId / sourcePath / section / score / container / lineStart /
    lineEnd) — the exact field set of the ``Citation`` model — or ``[]``.

    GRACEFUL: never raises. No answer, no map, no markers, or no resolvable
    marker → ``[]``. Does not read or modify the answer beyond regex scanning.
    """
    if not answer or not chunk_map:
        return []
    try:
        chunks = [c for c in chunk_map if isinstance(c, dict)]
        if not chunks:
            return []

        # Build lookup indices (first-wins preserves retrieval rank order).
        by_chunk_id: dict[str, dict[str, Any]] = {}
        by_path_base: dict[str, dict[str, Any]] = {}
        by_title: dict[str, dict[str, Any]] = {}
        for c in chunks:
            cid = str(c.get('chunkId') or '').strip()
            if cid and cid not in by_chunk_id:
                by_chunk_id[cid] = c
            base = _basename(c.get('sourcePath')).lower()
            if base and base not in by_path_base:
                by_path_base[base] = c
            title = str(c.get('title') or '').strip().lower()
            if title and title not in by_title:
                by_title[title] = c

        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _emit(chunk: dict[str, Any]) -> None:
            cid = str(chunk.get('chunkId') or '')
            dedup_key = cid or str(chunk.get('sourcePath') or '') + '|' + str(chunk.get('section') or '')
            if dedup_key in seen:
                return
            seen.add(dedup_key)
            meta = chunk.get('metadata') if isinstance(chunk.get('metadata'), dict) else {}
            resolved.append({
                'chunkId': chunk.get('chunkId'),
                'sourcePath': chunk.get('sourcePath'),
                'section': chunk.get('section'),
                'score': chunk.get('score'),
                'container': chunk.get('container'),
                'lineStart': meta.get('lineStart') if isinstance(meta, dict) else None,
                'lineEnd': meta.get('lineEnd') if isinstance(meta, dict) else None,
            })

        # Dialect A — literal [Chunk_ID] markers anywhere in the answer.
        for m in _BRACKET_TOKEN_RE.finditer(answer):
            token = m.group(1).strip()
            if _looks_like_chunk_id(token):
                chunk = by_chunk_id.get(token)
                if chunk is not None:
                    _emit(chunk)

        # Dialect B — LightRAG "### References" section: "- [n] Document Title".
        # Scope the scan to the tail AFTER the first "### References" heading so a
        # bare "- [1] Some Title" line elsewhere in the prose is not mistaken for a
        # citation (the heading is the only anchor for this dialect). No heading →
        # no References citations (dialect A's '#'-bearing markers still scan whole).
        # Title matched against retrieved chunk title, else sourcePath basename.
        heading = _REFERENCES_HEADING_RE.search(answer)
        references_tail = answer[heading.end():] if heading is not None else ''
        for m in _REF_LINE_RE.finditer(references_tail):
            title = m.group(2).strip()
            key = title.lower()
            chunk = by_title.get(key) or by_path_base.get(_basename(title).lower())
            if chunk is None and key:
                # Loose contains-match against sourcePath basenames (title may be
                # a humanized file name). First retrieval-rank hit wins.
                for base, c in by_path_base.items():
                    if base and (base in key or key in base):
                        chunk = c
                        break
            if chunk is not None:
                _emit(chunk)

        return resolved[:max_citations]
    except Exception:  # pragma: no cover - defensive: citations must never break /query
        return []


# ── 3. Fallback template rendering ──────────────────────────────────────────


def render_fallback_template(
    template: str | None,
    context: dict[str, Any] | None = None,
) -> str | None:
    """Render an opt-in fallback interception body.

    Returns None when ``template`` is unconfigured (None or empty/whitespace) —
    callers MUST treat None as "feature off, preserve current behavior". When a
    template is configured, ``{placeholder}`` tokens are substituted from
    ``context`` (missing keys left as the literal ``{key}`` token, never raising
    KeyError). Non-string template → None (defensive).
    """
    if not isinstance(template, str):
        return None
    if not template.strip():
        return None
    ctx = context or {}

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:  # leave unknown tokens intact
            return '{' + key + '}'

    try:
        return template.format_map(_SafeDict(ctx))
    except Exception:  # pragma: no cover - malformed template must not break callers
        return template
