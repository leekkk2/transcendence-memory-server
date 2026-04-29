#!/usr/bin/env python3
"""Rebuild canonical LanceDB chunks from server-side sources."""
from __future__ import annotations

import argparse
import json
import re
import shutil
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


# 所有 row 的统一 schema 字段集 — 保证 streaming add 时 schema 一致
_ROW_SCHEMA_FIELDS: tuple[str, ...] = (
    'chunkId', 'taskId', 'docType', 'sourcePath', 'section', 'text',
    'container', 'title', 'source', 'tags', 'metadata',
)


def _normalize_metadata_inplace(row: dict[str, Any]) -> None:
    """metadata 统一序列化为 JSON 字符串，避免 LanceDB 嵌套 struct schema 不兼容。

    历史问题（2026-04-29）：不同 docType 的 metadata 字段不同（task_card 含
    project/status；client_ingest 含 kind/taskId），LanceDB 第一次建表时按首条
    row 推断嵌套 struct schema，后续 add 不同 keys 的 row 会抛
    `Invalid input, field 'X' does not exist in table schema`。
    解决：metadata 列类型统一为 string，反序列化推迟到查询端按需处理。
    """
    raw = row.get('metadata')
    if isinstance(raw, str):
        return
    if isinstance(raw, dict):
        row['metadata'] = json.dumps(raw, ensure_ascii=False, default=str)
    elif raw is None:
        row['metadata'] = '{}'
    else:
        try:
            row['metadata'] = json.dumps(raw, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            row['metadata'] = '{}'


def _normalize_row(row: dict[str, Any], container: str) -> dict[str, Any]:
    """补齐缺失字段，保证混合来源（task_card / memory / client_ingest）的 schema 一致。"""
    item: dict[str, Any] = {}
    for field in _ROW_SCHEMA_FIELDS:
        item[field] = row.get(field)
    item['container'] = container
    if item.get('tags') is None:
        item['tags'] = []
    if item.get('title') is None:
        item['title'] = ''
    if item.get('source') is None:
        item['source'] = ''
    _normalize_metadata_inplace(item)
    return item


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


def rebuild_rows(
    container: str,
    fresh_rows: list[dict[str, Any]],
    batch_size: int = 50,
) -> dict[str, int]:
    """Batched overwrite: 第一批用 mode='overwrite' 重置 schema，后续 batch 用 add。

    设计要点（基于 2026-04-29 OOM 复盘）：
    - **batched write**：每批 ~batch_size 条 vector flush 到 table，避免主进程
      持有所有 vector 列表（旧实现单进程峰值 RSS ~1 GB，叠加 retry churn 把
      宿主机推到 swap thrashing）。
    - **first-batch overwrite**：第一次 add 用 mode='overwrite' 清掉旧表，
      解决跨次 ingest 的 schema 不兼容问题（特别是 metadata 列从 struct → string
      的迁移）。
    - **retain rows 也 normalize**：旧表的 metadata 可能是 struct，统一转 string
      后再写回，保证整张表 schema 一致。
    - **失败时只保留部分行**：mode='overwrite' first batch 已删除旧数据，
      后续某个 batch add 失败会丢失剩余行——但比"完整 list 一次性写、失败前功尽弃"
      好：retry 期间内存是 batch 级（KB 量级）而不是整库级（GB 量级）。
    """
    db_dir = lancedb_dir(container)

    # 1. 加载并 normalize retain rows（不属于 REBUILD_DOC_TYPES 的旧行）
    retained_rows: list[dict[str, Any]] = []
    try:
        old_db = lancedb.connect(str(db_dir))
        old_table = old_db.open_table('chunks')
        for raw in old_table.to_arrow().to_pylist():
            item = dict(raw)
            item.pop('_distance', None)
            if str(item.get('docType') or '') in REBUILD_DOC_TYPES:
                continue
            _normalize_metadata_inplace(item)
            retained_rows.append(item)
    except Exception:
        pass

    if not fresh_rows and not retained_rows:
        return {'retained': 0, 'ingested': 0, 'total': 0}

    # 2. 直接 rmtree 旧 LanceDB 目录绕开 lancedb 0.30.x 的 drop_table /
    #    mode='overwrite' 偶发 listing.rs unreachable! panic（macOS RustPanic /
    #    Linux SIGABRT 不可 except）。retain 数据已经在内存里，下面会重新写回。
    chunks_path = db_dir / 'chunks.lance'
    if chunks_path.exists():
        try:
            shutil.rmtree(chunks_path)
        except Exception:
            pass

    # 3. 重新 connect 让 LanceDB 看到清空后的 path
    db = lancedb.connect(str(db_dir))

    ingested = 0
    table = None
    buf: list[dict[str, Any]] = []

    def _flush(items: list[dict[str, Any]]) -> None:
        nonlocal table
        if not items:
            return
        if table is None:
            table = db.create_table('chunks', data=items)
        else:
            table.add(items)

    # 2. 先 flush retained
    for row in retained_rows:
        buf.append(row)
        if len(buf) >= batch_size:
            _flush(buf)
            buf = []

    # 3. 再 embed + flush fresh rows
    for row in fresh_rows:
        item = _normalize_row(row, container)
        item['vector'] = embed_text(item['text']).tolist()
        buf.append(item)
        ingested += 1
        if len(buf) >= batch_size:
            _flush(buf)
            buf = []

    _flush(buf)

    if table is None:
        # buf 为空且 retained_rows + fresh_rows 都为空 — 上面已 early return
        # 安全兜底
        return {'retained': len(retained_rows), 'ingested': ingested, 'total': 0}

    total = int(table.count_rows())
    return {
        'retained': len(retained_rows),
        'ingested': ingested,
        'total': total,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', default='host-z')
    parser.add_argument('--memory-dir', default='')
    parser.add_argument('--archive-dir', default='')
    args = parser.parse_args()

    if args.container == 'host-z':
        default_memory = WS / 'memory-host-z'
        default_archive = WS / 'memory-archive-host-z'
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
