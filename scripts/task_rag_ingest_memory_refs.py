#!/usr/bin/env python3
"""Append memory references to tasks/rag/manifest.jsonl.

Scans memory/ and memory_archive/ for TASK-IDs and appends evidence chunks.

Usage:
  task_rag_ingest_memory_refs.py
"""
from __future__ import annotations
import json
import re
from pathlib import Path

import os

WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'
TASK_ID_RE = re.compile(r'TASK-\d{8}-\d{3}')


def manifest_path(container: str):
    base = TASKS / 'rag' / 'containers' / container
    base.mkdir(parents=True, exist_ok=True)
    return base / 'manifest.jsonl'


def iter_memory_files(memory_dir: Path, archive_dir: Path):
    for d in [memory_dir, archive_dir]:
        if not d.exists():
            continue
        for path in d.rglob('*.md'):
            yield path


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='imac')
    ap.add_argument('--memory-dir', default=str(WS/'memory'))
    ap.add_argument('--archive-dir', default=str(WS/'memory_archive'))
    args = ap.parse_args()

    manifest = manifest_path(args.container)
    memory_dir = Path(args.memory_dir)
    archive_dir = Path(args.archive_dir)

    with manifest.open('a', encoding='utf-8') as f:
        for path in iter_memory_files(memory_dir, archive_dir):
            text = path.read_text(encoding='utf-8', errors='ignore')
            ids = set(TASK_ID_RE.findall(text))
            if not ids:
                continue
            for task_id in ids:
                entry = {
                    'taskId': task_id,
                    'docType': 'memory_ref',
                    'sourcePath': str(path),
                    'chunkId': f"{task_id}#memory_ref#{path.stem}",
                    'section': 'memory_ref',
                    'text': text[:2000],
                    'tags': [],
                    'project': '',
                    'status': '',
                    'createdAt': '',
                    'updatedAt': ''
                }
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')

    print('ingested memory refs')


if __name__ == '__main__':
    main()
