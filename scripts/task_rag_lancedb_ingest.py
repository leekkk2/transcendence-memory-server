#!/usr/bin/env python3
"""Rebuild canonical LanceDB chunks from server-side sources."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import re
import time
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


def _is_metadata_string_schema(table) -> bool:
    """检查 LanceDB 表的 metadata 列是不是 string 类型（v0.5.4+ schema）。

    旧版（≤v0.5.2）将 metadata 直接存为嵌套 struct，schema 字段不一致会让 add()
    抛 `Invalid input, field 'X' does not exist in table schema`。
    """
    try:
        field = table.schema.field('metadata')
        type_str = str(field.type).lower()
        return 'string' in type_str
    except Exception:
        return False


_logger = logging.getLogger(__name__)
if not _logger.handlers and not logging.getLogger().handlers:
    import sys as _sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s', stream=_sys.stderr)


class IngestPacer:
    """自适应限速器：连续失败时指数退避 sleep（规避上游限速），
    连续成功时 sleep 收敛回 0。

    Why: gemini-embedding-001 等上游在突发并发下返回 429/5xx，但客户端
    无法直接探知"何时该慢下来"。通过 success/failure 信号自适应即可
    避免硬编码 sleep。

    用法：
        pacer = IngestPacer()
        for row in rows:
            try:
                embed(...)
                pacer.on_success()
            except Exception:
                pacer.on_failure()
            pacer.sleep_if_needed()
    """

    def __init__(self, threshold: int = 3, base: float = 2.0, max_delay: float = 30.0):
        self._threshold = threshold
        self._base = base
        self._max = max_delay
        self._consecutive_fails = 0
        self._success_streak = 0
        self.delay = 0.0

    def on_success(self) -> None:
        self._consecutive_fails = 0
        self._success_streak += 1
        if self._success_streak >= 5 and self.delay > 0:
            self.delay = max(0.0, self.delay / 2 - 0.5)
            self._success_streak = 0

    def on_failure(self) -> None:
        self._success_streak = 0
        self._consecutive_fails += 1
        if self._consecutive_fails >= self._threshold:
            self.delay = self._base if self.delay == 0 else min(self._max, self.delay * 2)

    def sleep_if_needed(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)


def _try_optimize(table) -> None:
    """合并 fragments + 清理旧版本，避免 LanceDB MVCC 累积膨胀盘占。

    历史教训（2026-04-29）：v0.5.2 之前 ingest 用 mode='overwrite' 创建新 dataset
    version 但保留旧 fragments，多次 ingest 后 yzjx 容器从 ~100 MB 实际数据
    膨胀到 2.0 GB 盘占。
    """
    try:
        # optimize() 内部 = compact_files() + cleanup_old_versions()
        # cleanup_older_than=0 立刻清掉所有旧版本（生产 OK，因为 ingest 是 batch 操作，
        # 没有 in-flight reader 还在读旧 version）
        table.optimize(cleanup_older_than=datetime.timedelta(seconds=0))
    except Exception:
        # fallback: 单独调，互不依赖
        try:
            table.compact_files()
        except Exception:
            pass
        try:
            table.cleanup_old_versions()
        except Exception:
            pass


def rebuild_rows(
    container: str,
    fresh_rows: list[dict[str, Any]],
    batch_size: int = 50,
) -> dict[str, int]:
    """In-place incremental rebuild + 自动 fragment 清理。

    设计要点（基于 2026-04-29 多次复盘）：
    - **不再 rmtree**：v0.5.4 用 shutil.rmtree 在 embed 失败时会丢索引（jsonl 是
      source of truth 没真丢，但用户感知是"数据没了"）。改为 in-place delete +
      add，失败时旧索引保留，下次 ingest 继续修复。
    - **delete REBUILD_DOC_TYPES + 增量 add**：retain 行原地保留，新行追加，
      不重写整表。
    - **batched flush**：每批 ~batch_size 条 flush，主进程峰值内存 ~KB 而非 GB。
    - **schema 不兼容自动 fallback**：旧表 metadata=struct 时 add() 会失败，
      自动切到 mode='overwrite' 完成一次性 schema migration（仅在跨大版本升级时触发）。
    - **末尾 optimize**：自动 compact_files + cleanup_old_versions，避免 MVCC
      fragments 累积（杜绝 yzjx 容器 100 MB 数据膨胀到 2 GB 的旧 bug）。
    """
    db = lancedb.connect(str(lancedb_dir(container)))

    # 1. 打开既有表，检查 schema 兼容性
    table = None
    schema_compatible = False
    try:
        table = db.open_table('chunks')
        schema_compatible = _is_metadata_string_schema(table)
    except Exception:
        table = None

    # 2. 如果旧 schema 不兼容（metadata=struct），需要做一次性 migration:
    #    把 retain 行读到内存 → mode='overwrite' 重建 schema 时一并写回
    retain_for_migration: list[dict[str, Any]] = []
    if table is not None and not schema_compatible:
        try:
            for raw in table.to_arrow().to_pylist():
                item = dict(raw)
                item.pop('_distance', None)
                if str(item.get('docType') or '') in REBUILD_DOC_TYPES:
                    continue
                _normalize_metadata_inplace(item)
                retain_for_migration.append(item)
        except Exception:
            pass
        # migration 标记：让 _flush 第一批走 mode='overwrite'
        table = None  # 重置，下面 _flush 第一次会重建

    # 3. schema 兼容时只删 REBUILD_DOC_TYPES 那部分，retain 行原地保留
    if table is not None and schema_compatible:
        rebuild_types = ", ".join(f"'{t}'" for t in REBUILD_DOC_TYPES)
        try:
            table.delete(f"docType IN ({rebuild_types})")
        except Exception:
            pass

    if not fresh_rows and not retain_for_migration:
        # 没有新数据也不需要 migration — 仅 compact + 返回
        if table is not None:
            _try_optimize(table)
            n = int(table.count_rows())
            return {'retained': n, 'ingested': 0, 'total': n}
        return {'retained': 0, 'ingested': 0, 'total': 0}

    ingested = 0
    buf: list[dict[str, Any]] = []
    is_first_flush = True

    def _flush(items: list[dict[str, Any]]) -> None:
        nonlocal table, is_first_flush
        if not items:
            return
        if table is None:
            # 表不存在 → 第一批 create_table（默认 mode='create'）
            # 如果是 schema migration 路径，用 mode='overwrite' 清掉旧 dataset
            mode = 'overwrite' if (is_first_flush and retain_for_migration) else 'create'
            table = db.create_table('chunks', data=items, mode=mode)
        else:
            table.add(items)
        is_first_flush = False

    # 4a. migration 模式下先 flush 旧 retain 行（schema 已 normalize 为 string）
    for row in retain_for_migration:
        buf.append(row)
        if len(buf) >= batch_size:
            _flush(buf)
            buf = []

    # 4b. Pass 1：embed + flush 新 fresh rows，单条失败记录到 retry queue 不阻塞
    pacer = IngestPacer(threshold=3, base=2.0, max_delay=30.0)
    failed_pass1: list[dict[str, Any]] = []
    HARD_FAIL_LIMIT = 20  # 连续 20 条失败说明上游真断了，整体 raise 让下次重跑
    consecutive_fails = 0

    for row in fresh_rows:
        item = _normalize_row(row, container)
        try:
            item['vector'] = embed_text(item['text']).tolist()
            pacer.on_success()
            consecutive_fails = 0
            buf.append(item)
            ingested += 1
            if len(buf) >= batch_size:
                _flush(buf)
                buf = []
        except Exception as exc:
            pacer.on_failure()
            consecutive_fails += 1
            failed_pass1.append({'row': row, 'error': str(exc)[:200]})
            if consecutive_fails >= HARD_FAIL_LIMIT:
                _flush(buf)
                if table is not None:
                    _try_optimize(table)
                raise RuntimeError(
                    f'Aborting ingest: {HARD_FAIL_LIMIT} consecutive embed failures, '
                    f'last: {exc}'
                )
            _logger.warning(
                'embed pass1 chunkId=%s failed (%d/%d consecutive): %s',
                row.get('chunkId'), consecutive_fails, HARD_FAIL_LIMIT, str(exc)[:120],
            )
        pacer.sleep_if_needed()

    _flush(buf)
    buf = []

    # 4c. Pass 2：失败队列固定间隔重试一次（绕开瞬时 5xx / 限速）
    skipped: list[dict[str, str]] = []
    if failed_pass1:
        _logger.info('pass2: retrying %d failed chunks at 5s interval', len(failed_pass1))
        for entry in failed_pass1:
            time.sleep(5.0)
            row = entry['row']
            item = _normalize_row(row, container)
            try:
                item['vector'] = embed_text(item['text']).tolist()
                buf.append(item)
                ingested += 1
                if len(buf) >= batch_size:
                    _flush(buf)
                    buf = []
            except Exception as exc:
                skipped.append({
                    'chunkId': str(item.get('chunkId') or ''),
                    'error': str(exc)[:200],
                })
                _logger.warning(
                    'embed pass2 chunkId=%s still failing, skipped: %s',
                    item.get('chunkId'), str(exc)[:120],
                )
        _flush(buf)

    # 5. 末尾 optimize 清旧 fragments
    if table is not None:
        _try_optimize(table)

    if table is None:
        return {
            'retained': len(retain_for_migration),
            'ingested': ingested,
            'skipped': len(skipped),
            'total': 0,
        }

    total = int(table.count_rows())
    return {
        'retained': max(0, total - ingested),
        'ingested': ingested,
        'skipped': len(skipped),
        'skipped_chunks': skipped[:20],  # 头 20 条便于排障，超过的省略
        'total': total,
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
