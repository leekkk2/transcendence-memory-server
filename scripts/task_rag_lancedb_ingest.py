#!/usr/bin/env python3
"""Rebuild canonical LanceDB chunks from server-side sources."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import lancedb

try:
    from task_rag_runtime import TASKS, WS, container_dir, embed_text, lancedb_dir
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.task_rag_runtime import TASKS, WS, container_dir, embed_text, lancedb_dir

# 配额感知静默重试 backlog / 容器索引状态机 —— 软依赖：3 个新模块同在 scripts/。
#
# embedding_errors 模块身份归一（与 task_rag_runtime / embedding_registry 同款）：
# worker subprocess（bare import）与 server（scripts. 前缀）两种入口可能各加载一份
# embedding_errors 副本 —— 两份 typed exception 类对象不同 → 跨副本 isinstance 失效
# → classify_embedding_error 内部 isinstance(exc, EmbeddingError) 落空 → 退化成裸
# 消息字符串启发式。这里复用任一已加载副本并登记到两个模块名下，保证全进程只有
# 一份类对象。task_rag_runtime 已在上面 import 时做过归一，此处显式再做一次，确保
# task_rag_lancedb_ingest 作为独立入口被 import 时也安全。
_embedding_errors = (
    sys.modules.get('scripts.embedding_errors')
    or sys.modules.get('embedding_errors')
)
if _embedding_errors is None:  # pragma: no cover - 取决于运行入口
    try:
        import embedding_errors as _embedding_errors  # type: ignore[no-redef]
    except ImportError:
        from scripts import embedding_errors as _embedding_errors  # type: ignore[no-redef]
sys.modules.setdefault('embedding_errors', _embedding_errors)
sys.modules.setdefault('scripts.embedding_errors', _embedding_errors)
classify_embedding_error = _embedding_errors.classify_embedding_error

# backlog / index-state 是本模块自有类，不涉及跨副本 isinstance —— 普通双 import。
try:
    from embed_backlog import BacklogStore  # type: ignore[import-not-found]
    from index_state import IndexStateStore  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.embed_backlog import BacklogStore  # type: ignore[import-not-found]
    from scripts.index_state import IndexStateStore  # type: ignore[import-not-found]


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
#
# v0.12.0 增量 embed：新增 2 个字段，作为 chunk 级断点续传的「检查点」标记：
#   - content_hash：sha256(传给 embed_text 的精确文本)，文本未变 + 向量自洽时跳过重嵌
#   - embedded_at：该 chunk 向量写入时的 epoch 秒，便于排障 / 索引新鲜度审计
# 迁移前老表无 content_hash 列 → _chunk_schema_has_content_hash False → 安全回退全量重嵌。
_ROW_SCHEMA_FIELDS: tuple[str, ...] = (
    'chunkId', 'taskId', 'docType', 'sourcePath', 'section', 'text',
    'container', 'title', 'source', 'tags', 'metadata',
    'embedding_model', 'embedding_dim', 'embedding_profile',
    'content_hash', 'embedded_at',
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
    # v0.12.0 增量 embed 检查点字段：embed 成功后由 rebuild_rows / embed_specific_chunks
    # 回填真实值；失败 / 历史 row 保留默认（空串 / 0），保证 schema 一致。
    if item.get('content_hash') is None:
        item['content_hash'] = ''
    if item.get('embedded_at') is None:
        item['embedded_at'] = 0
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


def _chunk_schema_has_content_hash(table) -> bool:
    """检查 LanceDB chunks 表是否已含 string 类型的 ``content_hash`` 列。

    增量 embed 的前提：只有迁移后的表才有 content_hash 检查点。迁移前老表
    `table.schema.field('content_hash')` 抛 KeyError → 返回 False → 增量逻辑
    安全回退到全量重嵌（见 ``rebuild_rows``）。

    用 pyarrow 类型严格判断（与 ``_is_metadata_string_schema`` 同款），避免
    误判 —— null 类型列（全 null 推断）不算合格的 content_hash 列。
    """
    try:
        import pyarrow as pa
        t = table.schema.field('content_hash').type
        return pa.types.is_string(t) or pa.types.is_large_string(t)
    except Exception:
        return False


def _compute_content_hash(text: str | None) -> str:
    """chunk 文本的内容指纹 —— 增量 embed 跳过判据的核心。

    哈希对象 = 传给 ``embed_text`` 的精确文本（``item['text']``）。文本逐字
    未变时 hash 命中，配合「向量非空 + 维度自洽」即可安全跳过重嵌。
    """
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


# ---- backlog / index-state 接入（契约 C3：db = WS/tasks/rag/queue.db）--------

def _incremental_enabled() -> bool:
    """``TM_INCREMENTAL_EMBED``（默认 "1"）—— 为 "0" 时回退旧全量重嵌行为。"""
    return os.environ.get('TM_INCREMENTAL_EMBED', '1') != '0'


def _backlog_enabled() -> bool:
    """``TM_BACKLOG_ENABLED``（默认 "1"）—— 为 "0" 时失败 chunk 不落 backlog。"""
    return os.environ.get('TM_BACKLOG_ENABLED', '1') != '0'


def _retry_batch_size() -> int:
    """``TM_BACKLOG_RETRY_BATCH``（默认 50）—— 定向重试的 claim / flush 批大小。"""
    try:
        return max(1, int(os.environ.get('TM_BACKLOG_RETRY_BATCH', '50')))
    except ValueError:
        return 50


def _queue_db_path() -> Path:
    """契约 C3：backlog / index-state 与 JobQueue 同库 ``WS/tasks/rag/queue.db``。

    动态读 ``WORKSPACE`` env（而非模块加载期的 ``WS``），兼容测试中 monkeypatch
    WORKSPACE 后重新解析。
    """
    ws = Path(os.environ.get('WORKSPACE') or WS)
    return ws / 'tasks' / 'rag' / 'queue.db'


def _get_backlog_store() -> BacklogStore:
    return BacklogStore(_queue_db_path())


def _get_index_state_store() -> IndexStateStore:
    return IndexStateStore(_queue_db_path())


def _record_index_state(
    container: str,
    *,
    total_objects: int,
    embedded_objects: int,
    succeeded: bool,
    last_error_class: str | None,
) -> None:
    """登记一次 embed run 结果到 ``container_index_state``。

    索引状态登记永不阻塞 ingest —— 任何失败只 warning 不抛。
    """
    try:
        _get_index_state_store().record_embed_run(
            container,
            total_objects=int(total_objects),
            embedded_objects=int(embedded_objects),
            succeeded=succeeded,
            last_error_class=last_error_class,
        )
    except Exception as exc:  # pragma: no cover - 状态登记尽力而为
        _logger.warning('index state record skipped: %s', exc)


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
    version 但保留旧 fragments，多次 ingest 后 my-container 容器从 ~100 MB 实际数据
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
    *,
    full_rebuild: bool = True,
) -> dict[str, Any]:
    """In-place incremental rebuild + 自动 fragment 清理。

    设计要点（基于 2026-04-29/30 多次复盘）：
    - **不再 rmtree**：v0.5.4 用 shutil.rmtree 在 embed 失败时会丢索引。改为
      in-place upsert，失败时旧索引保留，下次 ingest 继续修复。
    - **upsert via merge_insert + 末尾删孤儿**（v0.5.8 起）：全程不主动 delete
      旧行；用 merge_insert(chunkId) 幂等 upsert；所有写入完成后才删孤儿。
    - **batched flush**：每批 ~batch_size 条 flush，主进程峰值内存 ~KB 而非 GB。
    - **schema 不兼容自动 fallback**：旧表 metadata=struct 时切 mode='overwrite'
      完成一次性 schema migration。
    - **末尾 optimize**：自动 compact_files + cleanup_old_versions。

    v0.12.0 增量 embed + 配额感知静默重试 backlog：
    - **增量跳过**（``TM_INCREMENTAL_EMBED=1`` 默认常开）：Pass 1 前读现有 chunks
      表建 ``chunkId → (content_hash, vector_present, dim)`` 映射；文本 hash 命中
      + 向量非空 + 维度自洽 + 不在 backlog ``waiting/retrying`` 的 chunk 直接跳过
      不重嵌。迁移前老表（无 content_hash 列）安全回退全量重嵌。
    - **失败 chunk 进 backlog**（``TM_BACKLOG_ENABLED=1`` 默认常开）：Pass 2 仍
      失败的 chunk 调 ``BacklogStore.record_failure`` 持久化静默重试，不再丢进
      ``skipped`` 黑洞。HARD_FAIL_LIMIT 连续失败不再 raise 中止丢数据 —— 提前
      结束本轮，已失败 + 剩余未处理 chunk 全部落 backlog 待下轮。
    - **删孤儿安全**：守卫从 ``ingested > 0`` 改为 ``full_rebuild``（默认 True，
      保持 /embed 调用行为不变）；增量全跳过时 ``ingested==0`` 但仍须删真正被
      删除的对象。``embed_specific_chunks`` 永不走此路径。
    - **静默成功**：失败已全部落 backlog 时本函数不 raise，子进程退出码 0。
    """
    incremental = _incremental_enabled()
    backlog_on = _backlog_enabled()
    backlog_store = _get_backlog_store() if backlog_on else None

    db = lancedb.connect(str(lancedb_dir(container)))

    # 1. 打开既有表，检查 schema 兼容性
    table = None
    schema_compatible = False
    prior_table_existed = False
    needs_schema_migration = False
    try:
        table = db.open_table('chunks')
        prior_table_existed = True
        schema_compatible = _is_metadata_string_schema(table)
        # 迁移触发条件：metadata 非 string（≤v0.5.2 嵌套 struct），或缺 content_hash
        # 列（v0.12.0 增量 embed 新列）。实测当前 LanceDB add/merge_insert 不会自动
        # 扩展 schema —— 缺列必须走一次性 overwrite migration 补齐。
        needs_schema_migration = (
            not schema_compatible or not _chunk_schema_has_content_hash(table)
        )
    except Exception:
        table = None

    # 2. schema 需迁移 → 一次性 overwrite：retain 非 REBUILD 行（补齐新列默认值），
    #    REBUILD 行由 Pass 1 重新 embed 写回。
    retain_for_migration: list[dict[str, Any]] = []
    if table is not None and needs_schema_migration:
        try:
            for raw in table.to_arrow().to_pylist():
                item = dict(raw)
                item.pop('_distance', None)
                if str(item.get('docType') or '') in REBUILD_DOC_TYPES:
                    continue
                _normalize_metadata_inplace(item)
                # 补齐 v0.12.0 新列默认值，保证 overwrite 时 schema 一致
                item.setdefault('content_hash', '')
                item.setdefault('embedded_at', 0)
                retain_for_migration.append(item)
        except Exception:
            pass
        table = None  # 重置，_flush 第一次走 mode='overwrite' 重建

    # 2b. 增量 embed：构建 chunkId → (content_hash, vector_present, dim) 映射。
    #     仅在 schema 兼容且表已含 content_hash 列时启用；迁移前老表回退全量重嵌。
    existing_chunks: dict[str, tuple[str, bool, int]] = {}
    can_incremental = False
    if (
        incremental and table is not None and schema_compatible
        and _chunk_schema_has_content_hash(table)
    ):
        can_incremental = True
        try:
            for raw in table.to_arrow().to_pylist():
                cid = str(raw.get('chunkId') or '')
                if not cid:
                    continue
                vec = raw.get('vector')
                vec_present = vec is not None and len(vec) > 0
                existing_chunks[cid] = (
                    str(raw.get('content_hash') or ''),
                    vec_present,
                    len(vec) if vec_present else 0,
                )
        except Exception:
            # 读表失败 → 放弃增量，安全回退全量重嵌
            can_incremental = False
            existing_chunks = {}

    # backlog 中 waiting/retrying 的 chunk 不可跳过 —— 它们之前失败过，须重嵌补偿。
    backlog_pending: set[str] = set()
    if backlog_store is not None:
        try:
            for _st in ('waiting', 'retrying'):
                for _it in backlog_store.list_items(container, status=_st, limit=1_000_000):
                    backlog_pending.add(_it.chunk_id)
        except Exception:
            backlog_pending = set()

    if not fresh_rows and not retain_for_migration:
        # 没有新数据也不需要 migration — 仅 compact + 返回
        if table is not None:
            _try_optimize(table)
            n = int(table.count_rows())
            _record_index_state(
                container, total_objects=n, embedded_objects=n,
                succeeded=True, last_error_class=None,
            )
            return {'retained': n, 'ingested': 0, 'skipped_unchanged': 0,
                    'skipped': 0, 'backlog': 0, 'total': n}
        return {'retained': 0, 'ingested': 0, 'skipped_unchanged': 0,
                'skipped': 0, 'backlog': 0, 'total': 0}

    # v0.7.0：每次 rebuild 解析一次当前 container 的 embedding profile 元数据。
    # profile_dim 同时用于增量跳过判据「向量长度 == 当前 profile dim」。
    embedding_meta = _resolve_embedding_meta(container)
    profile_dim = int(embedding_meta[1] or 0)

    ingested = 0
    skipped_unchanged = 0
    buf: list[dict[str, Any]] = []
    is_first_flush = True
    resolved_ids: list[str] = []  # backlog 成员被重嵌成功 → 待 mark_resolved

    def _flush(items: list[dict[str, Any]]) -> None:
        nonlocal table, is_first_flush
        if not items:
            return
        if table is None:
            needs_overwrite = is_first_flush and prior_table_existed and needs_schema_migration
            mode = 'overwrite' if needs_overwrite else 'create'
            table = db.create_table('chunks', data=items, mode=mode)
        else:
            try:
                (table.merge_insert('chunkId')
                      .when_matched_update_all()
                      .when_not_matched_insert_all()
                      .execute(items))
            except Exception:
                # merge_insert 不可用时降级 add（safety net）
                table.add(items)
        is_first_flush = False

    # 4a. migration 模式下先 flush 旧 retain 行（schema 已 normalize 为 string）
    for row in retain_for_migration:
        buf.append(row)
        if len(buf) >= batch_size:
            _flush(buf)
            buf = []

    def _should_skip(row: dict[str, Any], text_hash: str) -> bool:
        """增量跳过判据：四个条件全满足才跳过该 chunk。

        content_hash 命中 + 向量非空 + 维度 == 当前 profile dim + 不在 backlog。
        任一不满足即重嵌 —— 防崩溃半写行被误判完成、防 profile 换过维度不符。
        """
        if not can_incremental:
            return False
        cid = str(row.get('chunkId') or '')
        info = existing_chunks.get(cid)
        if info is None:
            return False
        old_hash, vec_present, dim = info
        if old_hash != text_hash or not vec_present:
            return False
        # profile_dim 未知（registry 不可用）时不强制维度校验
        if profile_dim > 0 and dim != profile_dim:
            return False
        if cid in backlog_pending:
            return False
        return True

    # 4b. Pass 1：增量跳过未变 chunk；其余 embed + flush，失败留待 Pass 2
    pacer = IngestPacer(threshold=3, base=2.0, max_delay=30.0)
    failed: list[dict[str, Any]] = []  # 每项 {'row', 'hash', 'exc'}
    HARD_FAIL_LIMIT = 20  # 连续 20 条失败 = 上游真断了 → 提前结束本轮（不 raise）
    consecutive_fails = 0
    aborted_early = False
    remaining_after_abort: list[dict[str, Any]] = []

    for idx, row in enumerate(fresh_rows):
        item = _normalize_row(row, container)
        text_hash = _compute_content_hash(item['text'])
        item['content_hash'] = text_hash
        if _should_skip(row, text_hash):
            skipped_unchanged += 1
            continue
        try:
            # P0：文本文档摄取走 document 侧 asymmetric 前缀（title 一并带入）。
            item['vector'] = embed_text(
                item['text'], mode='document', title=item.get('title'),
            ).tolist()
            item['embedded_at'] = int(time.time())
            _annotate_embedding_meta(item, embedding_meta)
            pacer.on_success()
            consecutive_fails = 0
            buf.append(item)
            ingested += 1
            cid = str(item.get('chunkId') or '')
            if cid in backlog_pending:
                resolved_ids.append(cid)
            if len(buf) >= batch_size:
                _flush(buf)
                buf = []
        except Exception as exc:
            pacer.on_failure()
            consecutive_fails += 1
            failed.append({'row': row, 'hash': text_hash, 'exc': exc})
            _logger.warning(
                'embed pass1 chunkId=%s failed (%d consecutive): %s',
                row.get('chunkId'), consecutive_fails, str(exc)[:120],
            )
            if consecutive_fails >= HARD_FAIL_LIMIT:
                # 不再 raise 整体中止丢数据：提前结束，剩余 chunk 也落 backlog 待下轮。
                aborted_early = True
                remaining_after_abort = list(fresh_rows[idx + 1:])
                _logger.warning(
                    'embed pass1 aborting after %d consecutive failures; '
                    '%d remaining chunks deferred to backlog',
                    HARD_FAIL_LIMIT, len(remaining_after_abort),
                )
                break
        pacer.sleep_if_needed()

    _flush(buf)
    buf = []

    # 4c. Pass 2 / backlog 落账
    skipped: list[dict[str, str]] = []
    backlog_recorded = 0
    last_error_class: str | None = None

    def _to_backlog(chunk_id: str, exc: BaseException, content_hash: str) -> None:
        """把最终失败的 chunk 落 backlog 静默重试（契约 C1：classify error）。"""
        nonlocal backlog_recorded, last_error_class
        error_class = classify_embedding_error(exc)
        last_error_class = error_class
        backlog_recorded += 1
        if backlog_store is not None:
            try:
                backlog_store.record_failure(
                    container, chunk_id, error_class,
                    content_hash=content_hash,
                    last_error=str(exc)[:200],
                    retry_after=getattr(exc, 'retry_after', None),
                )
            except Exception as exc2:  # pragma: no cover - backlog 写入尽力而为
                _logger.warning('backlog record_failure skipped: %s', exc2)

    if aborted_early:
        # 上游真断了：已失败的 + 剩余未处理的全部落 backlog，跳过 Pass 2。
        abort_exc: BaseException = (
            failed[-1]['exc'] if failed else RuntimeError('upstream embedding unavailable')
        )
        for entry in failed:
            cid = str(entry['row'].get('chunkId') or '')
            skipped.append({'chunkId': cid, 'error': str(entry['exc'])[:200]})
            _to_backlog(cid, entry['exc'], entry['hash'])
        for row in remaining_after_abort:
            item = _normalize_row(row, container)
            cid = str(item.get('chunkId') or '')
            text_hash = _compute_content_hash(item['text'])
            skipped.append({'chunkId': cid, 'error': 'deferred: upstream embedding down'})
            _to_backlog(cid, abort_exc, text_hash)
    elif failed:
        # Pass 2：失败队列固定间隔重试一次（绕开瞬时 5xx / 限速）
        _logger.info('pass2: retrying %d failed chunks at 5s interval', len(failed))
        for entry in failed:
            time.sleep(5.0)
            row = entry['row']
            item = _normalize_row(row, container)
            text_hash = entry['hash']
            item['content_hash'] = text_hash
            cid = str(item.get('chunkId') or '')
            try:
                item['vector'] = embed_text(
                    item['text'], mode='document', title=item.get('title'),
                ).tolist()
                item['embedded_at'] = int(time.time())
                _annotate_embedding_meta(item, embedding_meta)
                buf.append(item)
                ingested += 1
                if cid in backlog_pending:
                    resolved_ids.append(cid)
                if len(buf) >= batch_size:
                    _flush(buf)
                    buf = []
            except Exception as exc:
                skipped.append({'chunkId': cid, 'error': str(exc)[:200]})
                _to_backlog(cid, exc, text_hash)
                _logger.warning(
                    'embed pass2 chunkId=%s still failing → backlog: %s',
                    cid, str(exc)[:120],
                )
        _flush(buf)
        buf = []

    # 5a. 末尾删孤儿（守卫从 ingested>0 改为 full_rebuild）。
    #     增量全跳过时 ingested==0 但 full_rebuild 仍要删真正被删除的对象。
    #     ingested_ids 取自 fresh_rows 全集（含跳过的）→ 仅删真正消失的对象。
    #     迁移过的表已由 overwrite 精确重写（无孤儿可能），跳过删孤儿。
    if table is not None and not needs_schema_migration and full_rebuild:
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

    # 5b. backlog 成员被重嵌成功 → mark_resolved（断点续传：已修复的不再重试）
    if backlog_store is not None and resolved_ids:
        try:
            backlog_store.mark_resolved_many(container, resolved_ids)
        except Exception as exc:  # pragma: no cover
            _logger.warning('backlog mark_resolved skipped: %s', exc)

    # 5c. optimize 清旧 fragments
    if table is not None:
        _try_optimize(table)

    total = int(table.count_rows()) if table is not None else 0
    # embedded_objects ≈ 总数 − backlog 中仍待重试的（waiting/retrying）。backlog
    # 空时 embedded==total → 状态机算出 fresh；非空时由状态机走 backlog 分支。
    backlog_active = 0
    if backlog_store is not None:
        try:
            counts = backlog_store.counts(container)
            backlog_active = counts.get('waiting', 0) + counts.get('retrying', 0)
        except Exception:
            backlog_active = 0
    _record_index_state(
        container,
        total_objects=total,
        embedded_objects=max(0, total - backlog_active),
        succeeded=(backlog_recorded == 0),
        last_error_class=last_error_class,
    )

    return {
        'retained': max(0, total - ingested),
        'ingested': ingested,
        'skipped_unchanged': skipped_unchanged,
        'skipped': len(skipped),
        'skipped_chunks': skipped[:20],  # 头 20 条便于排障，超过的省略
        'backlog': backlog_recorded,
        'total': total,
    }


def embed_specific_chunks(
    container: str,
    chunk_ids: list[str],
    *,
    memory_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
    backlog_store: BacklogStore | None = None,
) -> dict[str, Any]:
    """断点续传定向重试：只重嵌给定 ``chunk_ids``，merge_insert 幂等 upsert。

    与 ``rebuild_rows`` 的关键区别：
    - **只处理给定子集** —— 从 memory_objects.jsonl / task 卡 / memory 文档重新
      取文本，让被编辑过的对象用当前文本重试。
    - **永不触碰删孤儿** —— 安全性是结构性保证：本函数根本没有 delete 调用。
    - **每批成功 flush 后立即 ``mark_resolved_many``** —— backlog 即续传清单，
      崩溃后已 resolved 的不重做，未 resolved 的恢复继续。
    - 批内单 chunk 再失败 → ``record_failure``（attempts+1 自动重排退避），不
      影响同批其它 chunk。
    - 源对象已删除（chunk_id 在源里找不到）→ ``mark_resolved`` 防 backlog 无限堆积。

    供 ``--mode embed-backlog-retry`` CLI 与 worker drain tick 调用。
    """
    backlog_on = _backlog_enabled()
    if backlog_on and backlog_store is None:
        backlog_store = _get_backlog_store()
    if not backlog_on:
        backlog_store = None

    # 去重保序
    requested = list(dict.fromkeys(str(c) for c in chunk_ids if str(c)))
    if not requested:
        return {'container': container, 'requested': 0, 'embedded': 0,
                'failed': 0, 'missing': 0, 'total': 0}

    # 从全部来源重取文本（被编辑过的对象用当前文本重试）
    all_rows = _collect_fresh_rows(container, memory_dir, archive_dir)
    by_id: dict[str, dict[str, Any]] = {}
    for r in all_rows:
        cid = str(r.get('chunkId') or '')
        if cid:
            by_id[cid] = r

    missing = [c for c in requested if c not in by_id]
    if missing and backlog_store is not None:
        # 源对象已删除 —— 无可重试，标 resolved 防 backlog 无限堆积
        try:
            backlog_store.mark_resolved_many(container, missing)
        except Exception as exc:  # pragma: no cover
            _logger.warning('backlog mark_resolved(missing) skipped: %s', exc)

    targets = [c for c in requested if c in by_id]
    if not targets:
        return {'container': container, 'requested': len(requested), 'embedded': 0,
                'failed': 0, 'missing': len(missing), 'total': 0}

    db = lancedb.connect(str(lancedb_dir(container)))
    table = None
    try:
        table = db.open_table('chunks')
    except Exception:
        table = None

    embedding_meta = _resolve_embedding_meta(container)
    batch = _retry_batch_size()
    embedded = 0
    failed = 0
    last_error_class: str | None = None

    def _flush_retry(items: list[dict[str, Any]], ids: list[str]) -> None:
        """写一批 + 立即 mark_resolved（断点续传检查点）。"""
        nonlocal table
        if not items:
            return
        if table is None:
            table = db.create_table('chunks', data=items, mode='create')
        else:
            try:
                (table.merge_insert('chunkId')
                      .when_matched_update_all()
                      .when_not_matched_insert_all()
                      .execute(items))
            except Exception:
                table.add(items)
        # 每批成功 flush 后立即标 resolved —— 崩溃后已 resolved 的不重做。
        if backlog_store is not None and ids:
            try:
                backlog_store.mark_resolved_many(container, ids)
            except Exception as exc:  # pragma: no cover
                _logger.warning('backlog mark_resolved skipped: %s', exc)

    buf: list[dict[str, Any]] = []
    buf_ids: list[str] = []
    for cid in targets:
        row = by_id[cid]
        item = _normalize_row(row, container)
        text_hash = _compute_content_hash(item['text'])
        item['content_hash'] = text_hash
        try:
            item['vector'] = embed_text(
                item['text'], mode='document', title=item.get('title'),
            ).tolist()
            item['embedded_at'] = int(time.time())
            _annotate_embedding_meta(item, embedding_meta)
            buf.append(item)
            buf_ids.append(cid)
            embedded += 1
            if len(buf) >= batch:
                _flush_retry(buf, buf_ids)
                buf = []
                buf_ids = []
        except Exception as exc:
            failed += 1
            error_class = classify_embedding_error(exc)
            last_error_class = error_class
            if backlog_store is not None:
                try:
                    backlog_store.record_failure(
                        container, cid, error_class,
                        content_hash=text_hash,
                        last_error=str(exc)[:200],
                        retry_after=getattr(exc, 'retry_after', None),
                    )
                except Exception as exc2:  # pragma: no cover
                    _logger.warning('backlog record_failure skipped: %s', exc2)
            _logger.warning(
                'embed-backlog-retry chunkId=%s still failing → backlog: %s',
                cid, str(exc)[:120],
            )

    _flush_retry(buf, buf_ids)

    if table is not None:
        _try_optimize(table)

    total = int(table.count_rows()) if table is not None else 0
    _record_index_state(
        container,
        total_objects=total,
        embedded_objects=max(0, total - failed),
        succeeded=(failed == 0),
        last_error_class=last_error_class,
    )
    return {
        'container': container,
        'requested': len(requested),
        'embedded': embedded,
        'failed': failed,
        'missing': len(missing),
        'total': total,
    }


def ingest_precomputed_rows(
    container: str,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    """把已自带 `vector` 的 row 直接 upsert 进 LanceDB chunks 表。

    与 `rebuild_rows` 的区别：`rebuild_rows` 会对每条 row 重新跑 `embed_text`；
    本函数用于「向量已在上游算好」的场景（如多模态原生 embedding —— 媒体字节
    经 gemini-embedding-2 `:embedContent` 算出 3072 维向量后直接落库）。一个
    媒体项 = 一条 row。schema 经 `_normalize_row` 归一，与 `rebuild_rows` 完全
    一致（metadata string 化），保证同容器混排 + `/search` 反序列化兼容。

    幂等：按 chunkId 覆盖既有同 id 行。

    实现采用「读旧行 → 剔除同 chunkId → 合并新行 → `create_table(overwrite)`」，
    与 `task_rag_structured_ingest.upsert_records` 同一套久经验证的路径，**不用**
    `merge_insert` —— 后者在既有表上对带 3072 维 vector 列做 join 时会触发
    LanceDB DataFusion spill，在本服务环境实测抛 `Spill has sent an error`。
    多模态容器行数小（媒体项级别），整表 overwrite 成本可忽略。
    """
    if not rows:
        return {'ingested': 0, 'total': 0}
    materialized: list[dict[str, Any]] = []
    new_chunk_ids: set[str] = set()
    for row in rows:
        vector = row.get('vector')
        if vector is None:
            raise ValueError(f"row {row.get('chunkId')!r} missing precomputed vector")
        item = _normalize_row(row, container)
        item['vector'] = list(vector)
        materialized.append(item)
        new_chunk_ids.add(str(item.get('chunkId') or ''))

    # 旧行剔除同 chunkId（幂等覆盖），其余原样保留；旧行已含 vector + 归一 schema
    retained = [
        row for row in load_existing_rows(container)
        if str(row.get('chunkId') or '') not in new_chunk_ids
    ]
    merged = retained + materialized
    db = lancedb.connect(str(lancedb_dir(container)))
    table = db.create_table('chunks', data=merged, mode='overwrite')
    # 末尾 optimize：overwrite 会累积 dead fragments，与 upsert_records 一致清理
    _try_optimize(table)
    return {'ingested': len(materialized), 'total': int(table.count_rows())}


def _resolve_memory_dirs(
    container: str,
    memory_dir: str | Path | None,
    archive_dir: str | Path | None,
) -> tuple[Path, Path]:
    """解析 memory / archive 目录（与 main() 的 CLI 默认值逻辑一致）。"""
    if container == 'default':
        default_memory = WS / 'memory-default'
        default_archive = WS / 'memory-archive-default'
    else:
        default_memory = WS / 'memory'
        default_archive = WS / 'memory-archive'
    md = Path(memory_dir) if memory_dir else default_memory
    ad = Path(archive_dir) if archive_dir else default_archive
    return md, ad


def _collect_fresh_rows(
    container: str,
    memory_dir: str | Path | None = None,
    archive_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """汇总某容器的全部 fresh rows（task_card + memory + client_ingest）。

    供 ``embed_specific_chunks`` 定向重试时按 chunkId 重取文本，也供 ``main()``
    构造 /embed 全量输入。
    """
    md, ad = _resolve_memory_dirs(container, memory_dir, archive_dir)
    return (
        collect_cards()
        + collect_memory_docs(container, md, ad)
        + collect_memory_objects(container)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', default='default')
    parser.add_argument('--memory-dir', default='')
    parser.add_argument('--archive-dir', default='')
    # 契约 C2：--mode embed-backlog-retry 走断点续传定向重试（worker drain tick 构造）。
    parser.add_argument(
        '--mode', default='embed', choices=['embed', 'embed-backlog-retry'],
    )
    args = parser.parse_args()

    if args.mode == 'embed-backlog-retry':
        # claim 到期 backlog chunk → embed_specific_chunks 定向重试。
        # 同一个 BacklogStore 实例贯穿 claim → retry，避免二次构造时
        # _init_schema 把刚 claim 的 retrying 行误恢复成 waiting。
        store = _get_backlog_store()
        due = store.claim_due(args.container, _retry_batch_size())
        chunk_ids = [it.chunk_id for it in due]
        if not chunk_ids:
            print(json.dumps({
                'code': 0, 'container': args.container,
                'mode': 'embed-backlog-retry', 'claimed': 0,
                'embedded': 0, 'failed': 0, 'missing': 0, 'total': 0,
            }, ensure_ascii=False))
            return
        summary = embed_specific_chunks(
            args.container, chunk_ids,
            memory_dir=args.memory_dir or None,
            archive_dir=args.archive_dir or None,
            backlog_store=store,
        )
        summary.pop('container', None)
        print(json.dumps({
            'code': 0, 'container': args.container,
            'mode': 'embed-backlog-retry', 'claimed': len(chunk_ids),
            **summary,
        }, ensure_ascii=False))
        return

    memory_dir, archive_dir = _resolve_memory_dirs(
        args.container, args.memory_dir or None, args.archive_dir or None,
    )
    fresh_rows = _collect_fresh_rows(args.container, memory_dir, archive_dir)
    summary = rebuild_rows(args.container, fresh_rows)
    print(json.dumps({
        'code': 0,
        'container': args.container,
        'mode': 'embed',
        'rebuilt_doc_types': sorted(REBUILD_DOC_TYPES),
        'memory_dir': str(memory_dir),
        'archive_dir': str(archive_dir),
        **summary,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
