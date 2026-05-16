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
# v0.7.0 新增 3 个 embedding metadata 字段，便于多 model 共存时追溯每条 row 的 embedding 出处：
#   - embedding_model：实际调用的 model 名（如 'gemini-embedding-001'）
#   - embedding_dim：实际写入的 vector 维度（如 3072）
#   - embedding_profile：profiles.yaml 里 profile name（如 'gemini-3072'），便于反查路由配置
# 老 LanceDB 表无这 3 列时，PyArrow 读出来为 null，写入路径会按需做 schema migration（见 _is_metadata_string_schema）。
_ROW_SCHEMA_FIELDS: tuple[str, ...] = (
    'chunkId', 'taskId', 'docType', 'sourcePath', 'section', 'text',
    'container', 'title', 'source', 'tags', 'metadata',
    'embedding_model', 'embedding_dim', 'embedding_profile',
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
    """补齐缺失字段，保证混合来源（task_card / memory / client_ingest）的 schema 一致。

    v0.7.0：新增 embedding_model / embedding_dim / embedding_profile 字段。
    embed_text 调用前 row 还没有这些字段，默认空串 / 0；embed 后 _flush 路径
    通过 _annotate_embedding_meta() 回填真实值，便于按 model 追溯。
    """
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
    # v0.7.0 embedding metadata 默认值：embed 成功后会被覆盖；失败 / 历史 row 保留默认
    if item.get('embedding_model') is None:
        item['embedding_model'] = ''
    if item.get('embedding_dim') is None:
        item['embedding_dim'] = 0
    if item.get('embedding_profile') is None:
        item['embedding_profile'] = ''
    _normalize_metadata_inplace(item)
    return item


def _resolve_embedding_meta(container: str) -> tuple[str, int, str]:
    """查询当前 container 对应的 embedding profile 元数据（model / dim / profile_name）。

    v0.7.0 多 model 框架：profiles.yaml 加载后通过 embedding_registry 路由；
    若 v0.7.0 模块尚未上线（α 任务未合入或 yaml 不存在），按 legacy env 回退到空串占位。
    本函数从不抛异常 — 任何失败路径都返回三元组占位值，保证 ingest 不被元数据采集阻塞。
    """
    # 软导入：α 完成前 embedding_registry 不存在，老 env 路径仍可用
    try:
        from embedding_registry import get_registry  # type: ignore[import-not-found]
    except Exception:
        try:
            from scripts.embedding_registry import get_registry  # type: ignore[import-not-found]
        except Exception:
            return ('', 0, '')
    try:
        registry = get_registry()
        route = registry.resolve(container)
        profile = registry.get_profile(route.embedding)
        return (
            str(getattr(profile, 'model', '') or ''),
            int(getattr(profile, 'dim', 0) or 0),
            str(getattr(profile, 'name', '') or ''),
        )
    except Exception:
        return ('', 0, '')


def _annotate_embedding_meta(item: dict[str, Any], meta: tuple[str, int, str]) -> None:
    """把 (model, dim, profile_name) 回填到 row。仅当 item 未显式设值时覆盖。"""
    model, dim, profile = meta
    if not item.get('embedding_model'):
        item['embedding_model'] = model
    if not item.get('embedding_dim'):
        item['embedding_dim'] = dim
    if not item.get('embedding_profile'):
        item['embedding_profile'] = profile


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
    抛 `Invalid input, cannot cast field 'metadata' from Utf8 to Struct(...)`。

    Why: v0.5.5/0.5.6 用 `'string' in str(type).lower()` 判断，但
    `struct<kind: string>` 子串里也含 `'string'` → 误判为兼容 → add() 失败 →
    rows=0。改用 pyarrow.types.is_string / is_large_string 严格匹配。

    v0.7.0 注意：新增 embedding_model / embedding_dim / embedding_profile 3 列在
    老表中不存在，但 LanceDB merge_insert / add 路径会在首次写入时自动扩 schema（实测
    LanceDB ≥0.4 支持），因此此函数 *只* 判断 metadata 列是否 string 兼容；新 3 列
    的迁移由 LanceDB 表内部 schema evolution 处理，不在这里强制触发 overwrite。
    """
    try:
        import pyarrow as pa
        t = table.schema.field('metadata').type
        return pa.types.is_string(t) or pa.types.is_large_string(t)
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
    version 但保留旧 fragments，多次 ingest 后 team 容器从 ~100 MB 实际数据
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

    设计要点（基于 2026-04-29/30 多次复盘）：
    - **不再 rmtree**：v0.5.4 用 shutil.rmtree 在 embed 失败时会丢索引。改为
      in-place upsert，失败时旧索引保留，下次 ingest 继续修复。
    - **upsert via merge_insert + 末尾删孤儿**（v0.5.8 起）：旧版本"先 delete
      REBUILD_DOC_TYPES → 再 add"，若 embed 全部失败（限速 / 5xx 风暴）旧数据
      就被永久丢失（2026-04-30 team rows 从 5751 归 0 的根因）。改为：
        1) 全程不主动 delete 旧行
        2) 用 merge_insert(chunkId).when_matched_update_all() 写入，幂等
        3) 所有写入完成后才 delete 孤儿（docType IN REBUILD AND chunkId NOT IN ingested）
      ingest 任意阶段中断都不会丢已索引数据。
    - **batched flush**：每批 ~batch_size 条 flush，主进程峰值内存 ~KB 而非 GB。
    - **schema 不兼容自动 fallback**：旧表 metadata=struct 时 add() 会失败，
      自动切到 mode='overwrite' 完成一次性 schema migration（仅在跨大版本升级时触发）。
      v0.5.8 起 _is_metadata_string_schema 用 pyarrow 类型 ID 严格判断，避免
      `struct<kind: string>` 被字符串子串 contains 误判为兼容。
    - **末尾 optimize**：自动 compact_files + cleanup_old_versions，避免 MVCC
      fragments 累积。
    """
    db = lancedb.connect(str(lancedb_dir(container)))

    # 1. 打开既有表，检查 schema 兼容性
    table = None
    schema_compatible = False
    prior_table_existed = False
    try:
        table = db.open_table('chunks')
        prior_table_existed = True
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
        # migration 标记：让 _flush 第一批走 mode='overwrite'。
        # v0.5.9 fix: 即便 retain 为空（旧表全是 REBUILD_DOC_TYPES），仍需要
        # overwrite 才能避免 `Table 'chunks' already exists`（mode='create' 默认值）。
        table = None  # 重置，下面 _flush 第一次会重建

    # 3. v0.5.8: 不再立即 delete REBUILD_DOC_TYPES。改为末尾"删孤儿"模式，
    #    新数据通过 merge_insert upsert 写入，已索引数据失败时不会丢。

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
            # 表不存在 → 第一批 create_table（默认 mode='create'）。
            # schema migration 路径：旧表存在但不兼容（无论 retain 是否为空），
            # 用 mode='overwrite' 清掉旧 dataset 重建。
            # v0.5.9 fix: 之前条件 `retain_for_migration` 在旧表全是
            # REBUILD_DOC_TYPES 时为空，错误走 'create' → 表已存在 ValueError。
            needs_overwrite = is_first_flush and prior_table_existed and not schema_compatible
            mode = 'overwrite' if needs_overwrite else 'create'
            table = db.create_table('chunks', data=items, mode=mode)
        else:
            # v0.5.8: upsert by chunkId — 同 chunkId 时 update_all，否则 insert_all。
            # 这样并发或重试不会写出重复 chunkId，且不会破坏已有数据。
            try:
                (table.merge_insert('chunkId')
                      .when_matched_update_all()
                      .when_not_matched_insert_all()
                      .execute(items))
            except Exception:
                # merge_insert 不可用时降级 add（理论上不会触发，留作 safety net）
                table.add(items)
        is_first_flush = False

    # 4a. migration 模式下先 flush 旧 retain 行（schema 已 normalize 为 string）
    for row in retain_for_migration:
        buf.append(row)
        if len(buf) >= batch_size:
            _flush(buf)
            buf = []

    # 4b. Pass 1：embed + flush 新 fresh rows，单条失败记录到 retry queue 不阻塞
    # v0.7.0：每次 rebuild 解析一次当前 container 的 embedding profile 元数据，写入每条 row。
    # 解析失败时返回三元组占位（空串 / 0），保持 v0.6.x 行为不变。
    pacer = IngestPacer(threshold=3, base=2.0, max_delay=30.0)
    failed_pass1: list[dict[str, Any]] = []
    HARD_FAIL_LIMIT = 20  # 连续 20 条失败说明上游真断了，整体 raise 让下次重跑
    consecutive_fails = 0
    embedding_meta = _resolve_embedding_meta(container)

    for row in fresh_rows:
        item = _normalize_row(row, container)
        try:
            item['vector'] = embed_text(item['text']).tolist()
            _annotate_embedding_meta(item, embedding_meta)
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
                _annotate_embedding_meta(item, embedding_meta)
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

    # 5a. v0.5.8: 末尾删孤儿（仅在 schema 兼容路径，且确实写入了新数据时才执行）
    #     条件：upsert 后用 chunkId NOT IN ingested_set 把"旧的、不在新 fresh
    #     的 REBUILD_DOC_TYPES 行"清掉。fresh 全部失败时 ingested=0 → 跳过删除，
    #     旧数据自然保留。
    if table is not None and schema_compatible and ingested > 0:
        ingested_ids = [
            str(row.get('chunkId'))
            for row in fresh_rows
            if row.get('chunkId') is not None
        ]
        if ingested_ids:
            rebuild_types_in = ", ".join(f"'{t}'" for t in REBUILD_DOC_TYPES)
            # 防 SQL 注入（chunkId 由我们自己生成 task_id#section 等格式，但仍 escape 单引号）
            ids_in = ", ".join("'" + cid.replace("'", "''") + "'" for cid in ingested_ids)
            try:
                table.delete(
                    f"docType IN ({rebuild_types_in}) "
                    f"AND chunkId NOT IN ({ids_in})"
                )
            except Exception as exc:
                _logger.warning('orphan delete skipped: %s', str(exc)[:200])

    # 5b. optimize 清旧 fragments
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
