#!/usr/bin/env python3
"""Rebuild canonical LanceDB chunks from server-side sources."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import lancedb

try:
    from task_rag_runtime import TASKS, WS, container_dir, embed_text, lancedb_dir
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_runtime import TASKS, WS, container_dir, embed_text, lancedb_dir


REBUILD_DOC_TYPES = {'task_card', 'memory', 'client_ingest'}
SECTION_RE = re.compile(r'^##\s+(.+)$', re.M)


def memory_objects_path(container: str) -> Path:
    return container_dir(container) / 'memory_objects.jsonl'


def split_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    matches = list(SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((match.group(1).strip(), body))
    return sections


def parse_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    in_meta = False
    for line in text.splitlines():
        if line.strip() == '## Meta':
            in_meta = True
            continue
        if in_meta and line.startswith('## '):
            break
        if in_meta and line.strip().startswith('- '):
            try:
                key, value = line[2:].split(':', 1)
            except ValueError:
                continue
            meta[key.strip()] = value.strip()
    return meta


def collect_cards() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for folder in (TASKS / 'active', TASKS / 'archived'):
        if not folder.exists():
            continue
        for path in folder.rglob('TASK-*.md'):
            text = path.read_text(encoding='utf-8')
            meta = parse_meta(text)
            task_id = '-'.join(path.stem.split('-')[0:3])
            tags = [tag.strip() for tag in meta.get('Tags', '').split(',') if tag.strip()]
            for section, body in split_sections(text):
                records.append({
                    'chunkId': f'{task_id}#{section}',
                    'taskId': task_id,
                    'docType': 'task_card',
                    'sourcePath': str(path.relative_to(WS)),
                    'section': section,
                    'text': body,
                    'container': '',
                    'tags': tags,
                    'metadata': {
                        'project': meta.get('Project', ''),
                        'status': meta.get('Status', ''),
                        'createdAt': meta.get('Created', ''),
                        'updatedAt': meta.get('Updated', ''),
                    },
                })
    return records


def chunk_lines(text: str, size: int = 60, overlap: int = 10) -> list[str]:
    lines = text.splitlines()
    if not lines:
        return []
    step = max(1, size - overlap)
    chunks: list[str] = []
    for index in range(0, len(lines), step):
        chunk = '\n'.join(lines[index:index + size]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def iter_memory_files(memory_dir: Path, archive_dir: Path):
    for root in (memory_dir, archive_dir):
        if not root.exists():
            continue
        yield from root.rglob('*.md')


def collect_memory_docs(container: str, memory_dir: Path, archive_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in iter_memory_files(memory_dir, archive_dir):
        text = path.read_text(encoding='utf-8', errors='ignore')
        for index, chunk in enumerate(chunk_lines(text)):
            records.append({
                'chunkId': f'{path.stem}#{index}',
                'taskId': path.stem,
                'docType': 'memory',
                'sourcePath': str(path),
                'section': 'memory',
                'text': chunk,
                'container': container,
                'tags': [],
                'metadata': {},
            })
    return records


def build_object_text(payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    title = str(payload.get('title') or '').strip()
    text = str(payload.get('text') or '').strip()
    source = str(payload.get('source') or '').strip()
    tags = [str(tag).strip() for tag in payload.get('tags', []) if str(tag).strip()]
    metadata = payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {}

    if title:
        pieces.append(title)
    if text:
        pieces.append(text)
    if tags:
        pieces.append(f"tags: {' '.join(tags)}")
    if source:
        pieces.append(f'source: {source}')
    meta_lines = [f'{key}: {value}' for key, value in metadata.items() if value is not None]
    if meta_lines:
        pieces.append('\n'.join(meta_lines))
    return '\n\n'.join(piece for piece in pieces if piece)


def collect_memory_objects(container: str) -> list[dict[str, Any]]:
    path = memory_objects_path(container)
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        object_id = str(payload.get('id') or '').strip()
        text = build_object_text(payload)
        if not object_id or not text:
            continue
        records.append({
            'chunkId': f'{object_id}#client-ingest#{line_no}',
            'taskId': object_id,
            'docType': 'client_ingest',
            'sourcePath': str(path.relative_to(WS)),
            'section': 'client_ingest',
            'text': text,
            'container': container,
            'title': str(payload.get('title') or '').strip(),
            'source': str(payload.get('source') or '').strip(),
            'tags': [str(tag).strip() for tag in payload.get('tags', []) if str(tag).strip()],
            'metadata': payload.get('metadata') if isinstance(payload.get('metadata'), dict) else {},
        })
    return records


def load_existing_rows(container: str) -> list[dict[str, Any]]:
    db = lancedb.connect(str(lancedb_dir(container)))
    try:
        table = db.open_table('chunks')
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for row in table.to_arrow().to_pylist():
        item = dict(row)
        item.pop('_distance', None)
        rows.append(item)
    return rows


def rebuild_rows(container: str, fresh_rows: list[dict[str, Any]]) -> dict[str, int]:
    existing_rows = load_existing_rows(container)
    retained_rows = [
        row for row in existing_rows
        if str(row.get('docType') or '') not in REBUILD_DOC_TYPES
    ]
    materialized_rows = []
    for row in fresh_rows:
        item = dict(row)
        item['container'] = container
        item['vector'] = embed_text(item['text']).tolist()
        materialized_rows.append(item)

    merged_rows = retained_rows + materialized_rows
    if merged_rows:
        db = lancedb.connect(str(lancedb_dir(container)))
        db.create_table('chunks', data=merged_rows, mode='overwrite')
    return {
        'retained': len(retained_rows),
        'ingested': len(materialized_rows),
        'total': len(merged_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', default='imac')
    parser.add_argument('--memory-dir', default='')
    parser.add_argument('--archive-dir', default='')
    args = parser.parse_args()

    if args.container == 'imac':
        default_memory = WS / 'memory-imac'
        default_archive = WS / 'memory-archive-imac'
    else:
        default_memory = WS / 'memory'
        default_archive = WS / 'memory-archive'

    memory_dir = Path(args.memory_dir) if args.memory_dir else default_memory
    archive_dir = Path(args.archive_dir) if args.archive_dir else default_archive
    fresh_rows = (
        collect_cards()
        + collect_memory_docs(args.container, memory_dir, archive_dir)
        + collect_memory_objects(args.container)
    )
    summary = rebuild_rows(args.container, fresh_rows)
    print(json.dumps({
        'code': 0,
        'container': args.container,
        'rebuilt_doc_types': sorted(REBUILD_DOC_TYPES),
        'memory_dir': str(memory_dir),
        'archive_dir': str(archive_dir),
        **summary,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
