"""Bounded dense and title candidate retrieval using LanceDB's scalar filters."""
from __future__ import annotations

from typing import Any


_TITLE_CANDIDATE_LIMIT = 20


def search_candidates(table: Any, vector: Any, query: str, topk: int) -> list[dict]:
    """Keep dense hits and add title matches before the server reranks them.

    Title filtering happens *before* vector search, so matching titles cannot
    be crowded out by unrelated dense hits. Exact titles get the first slots,
    followed by literal, case-insensitive substrings (including Chinese).
    Every candidate retains LanceDB's real squared L2 distance; title matching
    never fabricates a vector score. No index rebuild or FTS tokenizer is needed.
    """
    rows = table.search(vector).metric('l2').limit(topk).to_list()
    query = query.strip()
    if not query or 'title' not in table.schema.names:
        return rows

    # DataFusion string literals escape apostrophes by doubling them. contains()
    # treats %, _ and backslashes literally, unlike a SQL LIKE pattern.
    literal = query.replace("'", "''")
    exact = f"lower(title) = lower('{literal}')"
    phrase = f"contains(lower(title), lower('{literal}')) AND NOT ({exact})"
    title_limit = min(topk, _TITLE_CANDIDATE_LIMIT)
    title_rows: list[dict] = []
    for predicate in (exact, phrase):
        remaining = title_limit - len(title_rows)
        if remaining <= 0:
            break
        title_rows.extend(
            table.search(vector).metric('l2')
            .where(predicate, prefilter=True).limit(remaining).to_list()
        )

    seen = {(row.get('taskId') or '', row.get('chunkId') or '') for row in rows}
    for row in title_rows:
        key = (row.get('taskId') or '', row.get('chunkId') or '')
        if key != ('', '') and key in seen:
            continue
        rows.append(row)
        seen.add(key)
    return rows
