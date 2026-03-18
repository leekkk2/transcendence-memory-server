#!/usr/bin/env python3
"""Build tasks/rag/manifest.jsonl from task cards.

- Parses task cards
- Emits section-level chunks with metadata

Usage:
  task_rag_build_manifest.py
"""
from __future__ import annotations
import json
import re
from pathlib import Path
from datetime import datetime

import os

WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'

# container-aware output

def manifest_path(container: str):
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
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
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
        task_id = path.stem.split('-')[0:3]
        task_id = '-'.join(task_id)
        yield path, task_id, meta, text


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='imac')
    args = ap.parse_args()

    entries = []
    for folder in [TASKS/'active', TASKS/'archived']:
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
                    'tags': [t.strip() for t in meta.get('Tags','').split(',') if t.strip()],
                    'project': meta.get('Project',''),
                    'status': meta.get('Status',''),
                    'createdAt': meta.get('Created',''),
                    'updatedAt': meta.get('Updated','')
                })

    manifest = manifest_path(args.container)
    with manifest.open('w', encoding='utf-8') as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')

    print(f"manifest: {manifest} ({len(entries)} chunks)")


if __name__ == '__main__':
    main()
