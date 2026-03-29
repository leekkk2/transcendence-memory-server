#!/usr/bin/env python3
"""Build tasks/rag/manifest.jsonl from server-side sources.

- Parses task cards
- Includes persisted client-ingest JSONL objects
- Emits chunk-level manifest entries with metadata

Usage:
  task_rag_build_manifest.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import os

WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'


def manifest_path(container: str) -> Path:
    base = TASKS / 'rag' / 'containers' / container
    base.mkdir(parents=True, exist_ok=True)
    return base / 'manifest.jsonl'


SECTION_RE = re.compile(r'^##\s+(.+)$', re.M)


def split_sections(text: str):
    sections = []
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections.append((title, body))
    return sections


def parse_meta(text: str):
    meta = {}
    in_meta = False
    for line in text.splitlines():
        if line.strip() == '## Meta':
            in_meta = True
            continue
        if in_meta and line.startswith('## '):
            break
        if in_meta and line.strip().startswith('- '):
            try:
                key, val = line[2:].split(':', 1)
                meta[key.strip()] = val.strip()
            except ValueError:
                pass
    return meta


def collect_cards(root: Path):
    for path in root.rglob('TASK-*.md'):
        text = path.read_text(encoding='utf-8')
        meta = parse_meta(text)
        task_id = '-'.join(path.stem.split('-')[0:3])
        yield path, task_id, meta, text


def collect_client_ingest(container: str):
    base = TASKS / 'rag' / 'containers' / container
    if not base.exists():
        return

    for path in sorted(base.glob('client-ingest-*.jsonl')):
        with path.open(encoding='utf-8') as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue

                object_id = str(obj.get('id', '')).strip()
                text = str(obj.get('text', '')).strip()
                if not object_id or not text:
                    continue

                metadata = obj.get('metadata')
                if not isinstance(metadata, dict):
                    metadata = {}
                tags = obj.get('tags')
                if not isinstance(tags, list):
                    tags = []

                title = str(obj.get('title') or '').strip()
                source = str(obj.get('source') or '').strip()
                metadata_text_parts = []
                for key in ('project', 'kind', 'status', 'source'):
                    value = metadata.get(key)
                    if value is not None:
                        value_str = str(value).strip()
                        if value_str:
                            metadata_text_parts.append(f"{key}: {value_str}")
                if source and not any(part == f"source: {source}" for part in metadata_text_parts):
                    metadata_text_parts.append(f"source: {source}")
                tag_text = ' '.join(str(t).strip() for t in tags if str(t).strip())
                chunk_parts = []
                if title:
                    chunk_parts.append(title)
                chunk_parts.append(text)
                if tag_text:
                    chunk_parts.append(f"tags: {tag_text}")
                if metadata_text_parts:
                    chunk_parts.append('\n'.join(metadata_text_parts))
                chunk_text = '\n\n'.join(part for part in chunk_parts if part)

                yield {
                    'taskId': object_id,
                    'docType': 'client_ingest',
                    'sourcePath': str(path.relative_to(WS)),
                    'chunkId': f"{object_id}#client-ingest#{line_no}",
                    'section': 'client_ingest',
                    'text': chunk_text,
                    'tags': [str(t).strip() for t in tags if str(t).strip()],
                    'project': str(metadata.get('project', '')) if metadata.get('project') is not None else '',
                    'status': str(metadata.get('status', '')) if metadata.get('status') is not None else '',
                    'createdAt': str(metadata.get('createdAt', '')) if metadata.get('createdAt') is not None else '',
                    'updatedAt': str(metadata.get('updatedAt', '')) if metadata.get('updatedAt') is not None else '',
                    'title': title,
                    'source': source,
                    'metadata': metadata,
                }


def main():
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='imac')
    args = ap.parse_args()

    entries = []
    for folder in [TASKS / 'active', TASKS / 'archived']:
        if not folder.exists():
            continue
        for path, task_id, meta, text in collect_cards(folder):
            sections = split_sections(text)
            for title, body in sections:
                entries.append({
                    'taskId': task_id,
                    'docType': 'task_card',
                    'sourcePath': str(path.relative_to(WS)),
                    'chunkId': f"{task_id}#{title}",
                    'section': title,
                    'text': body,
                    'tags': [t.strip() for t in meta.get('Tags', '').split(',') if t.strip()],
                    'project': meta.get('Project', ''),
                    'status': meta.get('Status', ''),
                    'createdAt': meta.get('Created', ''),
                    'updatedAt': meta.get('Updated', ''),
                })

    for entry in collect_client_ingest(args.container):
        entries.append(entry)

    manifest = manifest_path(args.container)
    with manifest.open('w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')

    print(f"manifest: {manifest} ({len(entries)} chunks)")


if __name__ == '__main__':
    main()
