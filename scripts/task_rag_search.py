#!/usr/bin/env python3
"""Search task RAG store from LanceDB."""
from __future__ import annotations

import argparse
import json

import lancedb

try:
    from task_rag_runtime import embed_text, lancedb_dir
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_runtime import embed_text, lancedb_dir


def _table_names(db) -> list[str]:
    try:
        raw = db.list_tables()
    except Exception:
        return []

    names: list[str] = []
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, (list, tuple)) and item:
            names.append(str(item[0]))
        elif isinstance(item, dict):
            names.append(str(item.get('name') or item.get('table_name') or ''))
        else:
            name = getattr(item, 'name', '')
            if name:
                names.append(str(name))
    return [name for name in names if name]


def search_lancedb(query: str, topk: int, container: str) -> dict[str, object]:
    db = lancedb.connect(str(lancedb_dir(container)))
    if 'chunks' not in set(_table_names(db)):
        try:
            table = db.open_table('chunks')
        except Exception:
            return {
                'code': 'container_not_initialized',
                'message': f"Container '{container}' has no searchable LanceDB table yet. Run /embed first.",
                'container': container,
                'initialized': False,
                'results': [],
            }
    else:
        table = db.open_table('chunks')
    vector = embed_text(query)
    cleaned: list[dict[str, object]] = []
    for row in table.search(vector).limit(topk).to_list():
        item = dict(row)
        distance = item.pop('_distance', None)
        item.pop('vector', None)
        if distance is not None:
            item['score'] = float(distance)
        cleaned.append(item)
    return {
        'code': 'ok',
        'container': container,
        'initialized': True,
        'results': cleaned,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', required=True)
    parser.add_argument('--topk', type=int, default=5)
    parser.add_argument('--container', default='imac')
    args = parser.parse_args()

    print(json.dumps(search_lancedb(args.query, args.topk, args.container), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
