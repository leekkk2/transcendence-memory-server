#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

TEXT_SUFFIXES = {
    '.md', '.markdown', '.txt', '.log', '.json', '.jsonl', '.yml', '.yaml',
    '.toml', '.ini', '.cfg', '.conf', '.csv', '.tsv', '.sh', '.py', '.js',
    '.ts', '.tsx', '.jsx', '.sql', '.xml'
}

BINARY_METADATA_SUFFIXES = {
    '.tar', '.gz', '.tgz', '.zip', '.rar', '.7z', '.xz', '.bz2', '.lz4', '.zst',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.pdf', '.doc', '.docx', '.xls',
    '.xlsx', '.ppt', '.pptx', '.db', '.sqlite', '.sqlite3', '.parquet', '.arrow'
}

SKIP_DIRS = {'.git', '__pycache__', '.DS_Store'}
SKIP_PATH_PARTS = {'archive/services'}
MAX_TEXT_BYTES = 64 * 1024
MAX_FILES = 400


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def dump_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.jsonl.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    tmp.replace(path)


def iter_files(root: Path):
    count = 0
    for path in sorted(root.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if any(token in rel for token in SKIP_PATH_PARTS):
            continue
        yield path
        count += 1
        if count >= MAX_FILES:
            break


def object_id(prefix: str, rel: str) -> str:
    digest = hashlib.sha1(rel.encode('utf-8')).hexdigest()[:16]
    stem = rel.replace(os.sep, '__').replace('/', '__')
    return f'{prefix}-{stem}-{digest}'[:240]


def read_text_excerpt(path: Path) -> str:
    data = path.read_bytes()[:MAX_TEXT_BYTES]
    return data.decode('utf-8', errors='ignore').strip()


def build_text_record(origin_root: Path, path: Path) -> dict[str, Any]:
    rel = path.relative_to(origin_root).as_posix()
    excerpt = read_text_excerpt(path)
    text = f'{rel}\n\n{excerpt}'.strip()
    st = path.stat()
    return {
        'id': object_id('rclone-eva', rel),
        'title': rel,
        'text': text,
        'source': str(path),
        'tags': ['rclone', 'archive', 'eva', 'rag'],
        'metadata': {
            'origin_root': str(origin_root),
            'relative_path': rel,
            'source_kind': 'rclone-archive',
            'file_type': 'text',
            'size': st.st_size,
            'mtime': int(st.st_mtime),
        },
    }


def build_binary_record(origin_root: Path, path: Path) -> dict[str, Any]:
    rel = path.relative_to(origin_root).as_posix()
    st = path.stat()
    suffix = ''.join(path.suffixes) or path.suffix or 'unknown'
    text = f'{rel}\n\nBinary/archive artifact recorded for retrieval metadata only.\nsize={st.st_size}\nsuffix={suffix}'
    return {
        'id': object_id('rclone-eva-meta', rel),
        'title': rel,
        'text': text,
        'source': str(path),
        'tags': ['rclone', 'archive', 'eva', 'rag', 'metadata-only'],
        'metadata': {
            'origin_root': str(origin_root),
            'relative_path': rel,
            'source_kind': 'rclone-archive',
            'file_type': 'binary-metadata',
            'size': st.st_size,
            'mtime': int(st.st_mtime),
            'suffix': suffix,
        },
    }


def classify(path: Path) -> str | None:
    suffixes = ''.join(path.suffixes).lower()
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES or any(suffixes.endswith(ext) for ext in TEXT_SUFFIXES):
        return 'text'
    if suffix in BINARY_METADATA_SUFFIXES or any(suffixes.endswith(ext) for ext in BINARY_METADATA_SUFFIXES):
        return 'binary'
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--origin-root', required=True)
    ap.add_argument('--memory-objects', required=True)
    ap.add_argument('--prune-prefix', default='rclone-eva')
    args = ap.parse_args()

    origin_root = Path(args.origin_root).resolve()
    memory_objects = Path(args.memory_objects).resolve()

    existing = load_jsonl(memory_objects)
    kept = []
    for row in existing:
        row_id = str(row.get('id') or '')
        if row_id.startswith(args.prune_prefix):
            continue
        kept.append(row)

    generated: list[dict[str, Any]] = []
    text_count = 0
    binary_count = 0
    skipped = 0

    for path in iter_files(origin_root):
        kind = classify(path)
        if kind == 'text':
            generated.append(build_text_record(origin_root, path))
            text_count += 1
        elif kind == 'binary':
            generated.append(build_binary_record(origin_root, path))
            binary_count += 1
        else:
            skipped += 1

    rows = kept + generated
    dump_jsonl(memory_objects, rows)
    print(json.dumps({
        'code': 0,
        'origin_root': str(origin_root),
        'memory_objects': str(memory_objects),
        'kept_existing': len(kept),
        'generated': len(generated),
        'generated_text': text_count,
        'generated_binary_metadata': binary_count,
        'skipped': skipped,
        'total_rows': len(rows),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
