#!/usr/bin/env python3
"""Ingest structured JSON-like data into LanceDB."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import lancedb

try:
    from task_rag_lancedb_ingest import load_existing_rows
    from task_rag_runtime import embed_text, lancedb_dir
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_lancedb_ingest import load_existing_rows
    from scripts.task_rag_runtime import embed_text, lancedb_dir


SCALAR_KEYS_PRIORITY = [
    'title', 'name', 'label', 'url', 'href', 'description', 'summary',
    'content', 'text', 'value', 'type', 'id',
]


def path_to_str(path_parts: list[str]) -> str:
    return '/' + '/'.join(path_parts) if path_parts else '/'


def summarize_scalar(value: Any, max_len: int = 300) -> str:
    if isinstance(value, str):
        text = value.strip().replace('\n', ' ')
    else:
        text = json.dumps(value, ensure_ascii=False)
    return text[:max_len]


def collect_priority_fields(obj: dict[str, Any]) -> list[tuple[str, Any]]:
    seen = set()
    fields: list[tuple[str, Any]] = []
    for key in SCALAR_KEYS_PRIORITY:
        if key in obj and not isinstance(obj[key], (dict, list)):
            fields.append((key, obj[key]))
            seen.add(key)
    for key, value in obj.items():
        if key not in seen and not isinstance(value, (dict, list)):
            fields.append((key, value))
    return fields


def build_object_chunk(path_parts: list[str], obj: dict[str, Any], doc_type: str) -> str:
    lines = [f'DOC_TYPE: {doc_type}', f'PATH: {path_to_str(path_parts)}']
    for key, value in collect_priority_fields(obj)[:20]:
        lines.append(f'{key}: {summarize_scalar(value)}')
    child_keys = [key for key, value in obj.items() if isinstance(value, (dict, list))]
    if child_keys:
        lines.append('children: ' + ', '.join(child_keys[:20]))
    return '\n'.join(lines)


def build_scalar_chunk(path_parts: list[str], value: Any, doc_type: str) -> str:
    return '\n'.join([
        f'DOC_TYPE: {doc_type}',
        f'PATH: {path_to_str(path_parts)}',
        f'value: {summarize_scalar(value, max_len=1000)}',
    ])


def walk(value: Any, path_parts: list[str], records: list[dict[str, Any]], *, doc_id: str, doc_type: str, source_path: str):
    if isinstance(value, dict):
        records.append({
            'chunkId': f'{doc_id}#{len(records)}',
            'taskId': doc_id,
            'docType': doc_type,
            'sourcePath': source_path,
            'section': path_to_str(path_parts),
            'structuredPath': path_to_str(path_parts),
            'text': build_object_chunk(path_parts, value, doc_type),
            'container': '',
            'metadata': {},
        })
        for key, child in value.items():
            walk(child, path_parts + [str(key)], records, doc_id=doc_id, doc_type=doc_type, source_path=source_path)
        return

    if isinstance(value, list):
        preview = [f'[{index}] {summarize_scalar(item, 120)}' for index, item in enumerate(value[:10])]
        records.append({
            'chunkId': f'{doc_id}#{len(records)}',
            'taskId': doc_id,
            'docType': doc_type,
            'sourcePath': source_path,
            'section': path_to_str(path_parts),
            'structuredPath': path_to_str(path_parts),
            'text': '\n'.join([
                f'DOC_TYPE: {doc_type}',
                f'PATH: {path_to_str(path_parts)}',
                f'list_length: {len(value)}',
                'preview:',
                *preview,
            ]),
            'container': '',
            'metadata': {},
        })
        for index, item in enumerate(value):
            walk(item, path_parts + [str(index)], records, doc_id=doc_id, doc_type=doc_type, source_path=source_path)
        return

    records.append({
        'chunkId': f'{doc_id}#{len(records)}',
        'taskId': doc_id,
        'docType': doc_type,
        'sourcePath': source_path,
        'section': path_to_str(path_parts),
        'structuredPath': path_to_str(path_parts),
        'text': build_scalar_chunk(path_parts, value, doc_type),
        'container': '',
        'metadata': {},
    })


def build_records(input_path: Path, doc_id: str | None, doc_type: str) -> tuple[str, list[dict[str, Any]]]:
    raw = json.loads(input_path.read_text(encoding='utf-8', errors='ignore'))
    stable_id = doc_id or hashlib.sha1(str(input_path).encode('utf-8')).hexdigest()
    records: list[dict[str, Any]] = []
    walk(raw, [], records, doc_id=stable_id, doc_type=doc_type, source_path=str(input_path))
    return stable_id, records


def upsert_records(container: str, doc_id: str, doc_type: str, records: list[dict[str, Any]]) -> dict[str, int]:
    retained = [
        row for row in load_existing_rows(container)
        if not (str(row.get('taskId') or '') == doc_id and str(row.get('docType') or '') == doc_type)
    ]
    materialized = []
    for record in records:
        item = dict(record)
        item['container'] = container
        item['vector'] = embed_text(item['text']).tolist()
        materialized.append(item)
    merged = retained + materialized
    if merged:
        db = lancedb.connect(str(lancedb_dir(container)))
        db.create_table('chunks', data=merged, mode='overwrite')
    return {
        'retained': len(retained),
        'ingested': len(materialized),
        'total': len(merged),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', default='eva')
    parser.add_argument('--input', required=True)
    parser.add_argument('--doc-id', default='')
    parser.add_argument('--doc-type', default='structured_json')
    args = parser.parse_args()

    input_path = Path(args.input)
    doc_id, records = build_records(input_path, args.doc_id or None, args.doc_type)
    summary = upsert_records(args.container, doc_id, args.doc_type, records)
    print(json.dumps({
        'code': 0,
        'container': args.container,
        'doc_id': doc_id,
        'doc_type': args.doc_type,
        'chunks': len(records),
        'input': str(input_path),
        **summary,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
