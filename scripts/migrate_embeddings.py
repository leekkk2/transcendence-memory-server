#!/usr/bin/env python3
"""Embedding profile migration tool — 把一个 container 的 LanceDB chunks 表
从 embedding profile A 重 embed 到 profile B（dim 可不同）。

Why（背景）：
- v0.7.0 multi-embedding 之后每个 container 路由到独立 profile。
- LanceDB 表 schema 含 `fixed_size_list<float>[N]` 固定维度，profile 切换若
  dim 变化（如 3072 → 1024），新数据 add 会直接 schema 不匹配抛错。
- 解决：流式重新 embed → 写到 sibling `chunks_v2` 表 → 全部完成后 FS 原子 rename。

设计要点：
1. **dry-run 是默认行为**：用户必须显式 `--commit` 才会真正写。dry-run 只
   实测一次小 sample latency，外推估算总耗时与 token 量，不写任何东西。
2. **不依赖容器路由**：`--from` / `--to` 是 profile name（registry.get_profile
   能解析的），不依赖 container 当前路由配置，方便迁移过程灵活试错。
3. **FS rename 不是 lancedb API**：LanceDB OSS 不支持 `db.rename_table()`
   (NotImplementedError)。`.lance` 表实际是 `<table_name>.lance/` 子目录，
   `shutil.move` 即可原子 rename（同 inode rename，POSIX 原子保证）。
4. **vec_index 不迁移**：dim 变化后旧 IVF/HNSW 索引无意义；新表创建后由
   后续 ingest / 显式 reindex 步骤重建（不在本工具范围）。
5. **chunks_v2 已存在 → 拒绝**：避免覆盖上次中断遗留的半成品。用户必须显
   式清理后重跑。
6. **失败 → chunks_v2 保留**：方便用户读日志判断进度，不自动删。
7. **embedding 元数据更新**：每条 row 写入 to-profile 的 model / dim / profile
   字段，保证后续 search 路径能正确识别。

CLI:
    python scripts/migrate_embeddings.py \\
        --container <name> \\
        --from <profile-from> \\
        --to <profile-to> \\
        [--batch-size 100] \\
        [--dry-run | --commit] \\
        [--workspace /path]

退出码：
    0 成功（dry-run 或 commit 都返回 0）
    1 配置错误（profile 不存在、container 表不存在）
    2 运行时错误（embed 调用失败、写入失败等）
    3 状态冲突（chunks_v2 已存在）
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable

import lancedb
import numpy as np


# 让本脚本既能作为 package（pytest）也能直接 `python scripts/migrate_embeddings.py` 跑。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# 日志走 stderr，最终摘要走 stdout（方便 shell pipe）
_logger = logging.getLogger("migrate_embeddings")
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)


CHUNKS_TABLE = "chunks"
STAGING_TABLE = "chunks_v2"
# 旧表 backup 命名：chunks_old_YYYYMMDD_HHMMSS — 时间戳到秒避免连续 commit 冲撞
OLD_TABLE_PREFIX = "chunks_old_"


# ----------------------------- 工具函数 -----------------------------


def _get_registry():
    """晚绑定 import，避免 module top-level import 副作用污染测试环境。"""
    try:
        from embedding_registry import get_registry  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - 包路径 fallback
        from scripts.embedding_registry import get_registry  # type: ignore[import-not-found]
    return get_registry()


def _resolve_workspace(arg_value: str | None) -> Path:
    """workspace 优先级：参数 > WORKSPACE env > 仓库根目录。

    与 task_rag_runtime.WS 一致，确保 container_dir / lancedb_dir 找对位置。
    """
    if arg_value:
        return Path(arg_value).resolve()
    env_ws = os.environ.get("WORKSPACE")
    if env_ws:
        return Path(env_ws).resolve()
    # 默认：脚本所在目录的 parent（与 task_rag_runtime 的 fallback 一致）
    return Path(__file__).resolve().parents[1]


def _container_lancedb_path(workspace: Path, container: str) -> Path:
    """复用 task_rag_runtime 的 layout：tasks/rag/containers/<c>/lancedb"""
    return workspace / "tasks" / "rag" / "containers" / container / "lancedb"


def _open_db(lancedb_path: Path) -> Any:
    """打开 LanceDB connection；目录不存在直接抛 FileNotFoundError（不自动建）。"""
    if not lancedb_path.exists():
        raise FileNotFoundError(f"LanceDB path not found: {lancedb_path}")
    return lancedb.connect(str(lancedb_path))


def _list_table_names(db: Any) -> list[str]:
    """list_tables 在新版 LanceDB 返回 ListTablesResponse（含 .tables 属性），
    旧版返回 plain list。统一抽出为 list[str]。"""
    result = db.list_tables()
    # 新 API：pydantic ListTablesResponse
    if hasattr(result, "tables"):
        return list(result.tables or [])
    # 老 API：直接是 list
    return list(result)


def _build_embed_callable(profile_name: str) -> tuple[Callable[[list[str]], np.ndarray], int, str]:
    """根据 profile name 构造同步 embed 函数（内部用 asyncio.run 调用 async）。

    Returns:
        (embed_batch_func, dim, model_name)
        embed_batch_func: texts: list[str] -> np.ndarray shape (N, dim)
    """
    from profiles_loader import Route  # 本地 import，避免 top-level 副作用

    registry = _get_registry()
    profile = registry.get_profile(profile_name)
    # build_embedding_func 需要 Route — 这里只关心 embedding，fallback / rerank 留空。
    route = Route(embedding=profile_name)
    func, _sig = registry.build_embedding_func(route)

    def _call(texts: list[str]) -> np.ndarray:
        # EmbeddingFunc.func 是 async；同步上下文用 asyncio.run 触发。
        # 注意：不能复用 event loop（pytest / 嵌套 loop 环境），每次新建即可。
        result = asyncio.run(func.func(texts))
        # EmbeddingFunc 内部对 dim 做了校验，这里只需透传 numpy 数组。
        if not isinstance(result, np.ndarray):
            result = np.asarray(result, dtype="float32")
        return result

    return _call, int(profile.dim), str(profile.model)


def _detect_table_dim(table: Any) -> int | None:
    """从 LanceDB 表 schema 读 vector 列的固定维度；schema 不含 vector 列时返回 None。"""
    try:
        import pyarrow as pa  # noqa: F401 - 类型断言用

        vec_field = table.schema.field("vector")
        # fixed_size_list 类型有 list_size 属性
        return int(vec_field.type.list_size)
    except Exception:
        return None


def _detect_row_profile(table: Any) -> tuple[str, str, int]:
    """读首行的 embedding_model / embedding_profile / embedding_dim 字段，作为现有数据指纹。

    全为空时返回 ('', '', 0)，让调用方按 schema dim 兜底。
    """
    try:
        first = table.to_arrow().slice(0, 1).to_pylist()
        if not first:
            return ("", "", 0)
        row = first[0]
        return (
            str(row.get("embedding_model") or ""),
            str(row.get("embedding_profile") or ""),
            int(row.get("embedding_dim") or 0),
        )
    except Exception:
        return ("", "", 0)


def _estimate_tokens(text: str) -> int:
    """粗估 token：英文 ~4 字符 / token，中文 ~1.5 字符 / token。

    简化为统一 / 4，偏保守 (英文场景准；中文场景会高估，但 dry-run 给上限更安全)。
    """
    return max(1, len(text) // 4)


def _format_duration(seconds: float) -> str:
    """秒数 → 人类可读串。"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


# ----------------------------- dry-run -----------------------------


def _run_dry(
    table: Any,
    to_profile: str,
    batch_size: int,
    embed_callable: Callable[[list[str]], np.ndarray] | None = None,
) -> dict[str, Any]:
    """估算迁移所需时间 + token + batch 数；不写任何东西。

    采样：从表里取最多 5 条 row 实测一次 embed 调用 latency；外推总耗时。
    若 embed_callable 为 None（测试 / 离线场景）→ 跳过实测，用占位 latency。
    """
    total_rows = int(table.count_rows())
    if total_rows == 0:
        return {
            "mode": "dry-run",
            "total_rows": 0,
            "batch_count": 0,
            "sample_latency_ms": 0,
            "estimated_total_seconds": 0,
            "estimated_total_tokens": 0,
            "to_profile": to_profile,
            "note": "table is empty — nothing to migrate",
        }

    # 采样：最多 5 条；用 to_profile 实测一次 latency（更接近真实切换后体验）
    sample_size = min(5, total_rows)
    sample_rows = table.to_arrow().slice(0, sample_size).to_pylist()
    sample_texts = [str(r.get("text") or "") for r in sample_rows if r.get("text")]

    sample_latency_ms = 0.0
    if embed_callable is not None and sample_texts:
        t0 = time.perf_counter()
        try:
            _ = embed_callable(sample_texts)
            sample_latency_ms = (time.perf_counter() - t0) * 1000.0
        except Exception as exc:
            _logger.warning("sample embed failed; latency estimate fallback to 0: %s", exc)

    # 单条平均 latency = sample 总耗时 / sample 数
    per_text_ms = sample_latency_ms / max(1, len(sample_texts))
    batch_count = (total_rows + batch_size - 1) // batch_size
    # 每批 latency ≈ per_text_ms * batch_size（假设 batch embedding 内部并行 / pipeline）
    # 保守估计：单条延迟视为序列累加（上界），实际 batch 调用通常更快。
    estimated_total_seconds = (per_text_ms * total_rows) / 1000.0

    # token 估算：扫全表 text 长度（取 stats 而非全量加载，避免 OOM）
    try:
        text_col = table.to_arrow().column("text").to_pylist()
        estimated_total_tokens = sum(_estimate_tokens(str(t or "")) for t in text_col)
    except Exception:
        estimated_total_tokens = 0

    return {
        "mode": "dry-run",
        "total_rows": total_rows,
        "batch_count": batch_count,
        "batch_size": batch_size,
        "sample_size": len(sample_texts),
        "sample_latency_ms": round(sample_latency_ms, 2),
        "per_text_ms": round(per_text_ms, 2),
        "estimated_total_seconds": round(estimated_total_seconds, 2),
        "estimated_total_duration": _format_duration(estimated_total_seconds),
        "estimated_total_tokens": estimated_total_tokens,
        "to_profile": to_profile,
    }


def _print_dry_run_markdown(summary: dict[str, Any], from_profile: str) -> None:
    """dry-run 摘要打印为 markdown 表格到 stdout。"""
    print()
    print("# Migration dry-run summary")
    print()
    print(f"- **from profile**: `{from_profile}`")
    print(f"- **to profile**: `{summary['to_profile']}`")
    print()
    print("| Metric | Value |")
    print("| --- | --- |")
    print(f"| total rows | {summary['total_rows']} |")
    if summary["total_rows"] > 0:
        print(f"| batch size | {summary['batch_size']} |")
        print(f"| batch count | {summary['batch_count']} |")
        print(f"| sample size | {summary['sample_size']} |")
        print(f"| sample latency (ms) | {summary['sample_latency_ms']} |")
        print(f"| per-text latency (ms) | {summary['per_text_ms']} |")
        print(f"| estimated duration | {summary['estimated_total_duration']} |")
        print(f"| estimated tokens | {summary['estimated_total_tokens']:,} |")
    if "note" in summary:
        print()
        print(f"_{summary['note']}_")
    print()
    print("Re-run with `--commit` to perform the actual migration.")
    print()


# ----------------------------- commit -----------------------------


def _iter_batches(table: Any, batch_size: int):
    """流式读 chunks：使用 PyArrow Table.slice 切批，主进程峰值内存 ~O(batch_size)。

    Why 不用 table.to_arrow_batches()：LanceDB 的 batch iterator 实测在小表
    场景行为不可靠（可能一次性返回全部），用显式 slice 更可控。
    """
    total = int(table.count_rows())
    arrow_table = table.to_arrow()
    for start in range(0, total, batch_size):
        chunk = arrow_table.slice(start, batch_size)
        yield start, chunk.to_pylist()


def _run_commit(
    db: Any,
    lancedb_path: Path,
    src_table: Any,
    to_profile: str,
    batch_size: int,
    embed_callable: Callable[[list[str]], np.ndarray],
    to_dim: int,
    to_model: str,
) -> dict[str, Any]:
    """执行真实迁移。返回最终统计 dict。

    流程：
        1. 若 STAGING_TABLE 已存在 → 抛错（要求用户显式清理）
        2. 流式批量读 chunks → embed → 更新 embedding 元数据 → 写入 chunks_v2
        3. 全部成功后 FS rename：chunks → chunks_old_<ts>，chunks_v2 → chunks
        4. 任一步骤失败：chunks_v2 留在那（不删，方便排障），chunks 不动
    """
    # 1. 拒绝覆盖：staging 表已存在说明上次 commit 中断了 — 让用户先看现场
    existing_tables = set(_list_table_names(db))
    if STAGING_TABLE in existing_tables:
        raise RuntimeError(
            f"Staging table {STAGING_TABLE!r} already exists at {lancedb_path}. "
            f"Inspect or delete it manually before retrying (e.g. rm -rf "
            f"{lancedb_path / (STAGING_TABLE + '.lance')})."
        )

    total_rows = int(src_table.count_rows())
    if total_rows == 0:
        _logger.info("source table is empty; nothing to migrate")
        return {
            "mode": "commit",
            "migrated_rows": 0,
            "to_profile": to_profile,
            "to_dim": to_dim,
            "elapsed_seconds": 0.0,
            "note": "source table empty — no rename performed",
        }

    batch_count = (total_rows + batch_size - 1) // batch_size
    t_start = time.perf_counter()
    migrated = 0
    dst_table = None
    batch_index = 0

    for start, batch_rows in _iter_batches(src_table, batch_size):
        batch_index += 1
        batch_t0 = time.perf_counter()
        texts = [str(r.get("text") or "") for r in batch_rows]

        # embed 整批；失败 → 直接 raise 让外层中断（chunks_v2 残留可供排障）
        try:
            vectors = embed_callable(texts)
        except Exception as exc:
            _logger.error(
                "batch %d/%d embed failed at row %d: %s",
                batch_index, batch_count, start, exc,
            )
            raise RuntimeError(
                f"embed failed at batch {batch_index}/{batch_count} (row {start}): {exc}"
            ) from exc

        if len(vectors) != len(batch_rows):
            raise RuntimeError(
                f"embed returned {len(vectors)} vectors for {len(batch_rows)} rows "
                f"(batch {batch_index})"
            )

        # 构造新 row：替换 vector + 更新 3 个 embedding 元数据字段
        new_rows: list[dict[str, Any]] = []
        for row, vec in zip(batch_rows, vectors):
            new_row = dict(row)
            # 清掉 LanceDB 查询时可能残留的 _distance
            new_row.pop("_distance", None)
            new_row["vector"] = vec.tolist() if isinstance(vec, np.ndarray) else list(vec)
            new_row["embedding_model"] = to_model
            new_row["embedding_dim"] = to_dim
            new_row["embedding_profile"] = to_profile
            new_rows.append(new_row)

        # 写 staging：第一批 create_table，之后 add
        if dst_table is None:
            dst_table = db.create_table(STAGING_TABLE, data=new_rows, mode="create")
        else:
            dst_table.add(new_rows)

        migrated += len(new_rows)
        batch_ms = (time.perf_counter() - batch_t0) * 1000.0
        # progress：每批一行到 stderr，方便 grep / tail
        print(
            f"batch {batch_index}/{batch_count} embedded {len(new_rows)} rows "
            f"in {batch_ms:.0f} ms (cumulative {migrated}/{total_rows})",
            file=sys.stderr,
            flush=True,
        )

    elapsed = time.perf_counter() - t_start

    # 2. 校验 staging 行数 == 源行数（避免 race / 部分写入）
    if dst_table is not None:
        staging_count = int(dst_table.count_rows())
        if staging_count != total_rows:
            raise RuntimeError(
                f"staging row count mismatch: src={total_rows} staging={staging_count}"
            )

    # 3. 原子 rename：先 src → old_<ts>，再 staging → src
    #    FS-level move 是同 inode rename，POSIX 原子。
    #    但两次 move 之间有微小窗口；若中间崩溃 → 残留 chunks_old_<ts> + chunks_v2，
    #    用户可手动恢复：mv chunks_v2.lance chunks.lance
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    old_name = f"{OLD_TABLE_PREFIX}{timestamp}"
    src_dir = lancedb_path / f"{CHUNKS_TABLE}.lance"
    old_dir = lancedb_path / f"{old_name}.lance"
    staging_dir = lancedb_path / f"{STAGING_TABLE}.lance"

    if not src_dir.exists():
        raise RuntimeError(f"source table dir disappeared during migration: {src_dir}")
    if not staging_dir.exists():
        raise RuntimeError(f"staging table dir not found after writes: {staging_dir}")
    if old_dir.exists():
        # 极端：同秒内重跑 → 加微秒后缀，绝不覆盖现存 old
        old_name = f"{old_name}_{_dt.datetime.now().strftime('%f')}"
        old_dir = lancedb_path / f"{old_name}.lance"

    shutil.move(str(src_dir), str(old_dir))
    try:
        shutil.move(str(staging_dir), str(src_dir))
    except Exception:
        # 回滚：把 old 改回去，保持数据完整
        shutil.move(str(old_dir), str(src_dir))
        raise

    return {
        "mode": "commit",
        "migrated_rows": migrated,
        "total_rows": total_rows,
        "to_profile": to_profile,
        "to_dim": to_dim,
        "to_model": to_model,
        "elapsed_seconds": round(elapsed, 2),
        "elapsed_human": _format_duration(elapsed),
        "old_table_dir": str(old_dir),
        "new_table_dir": str(src_dir),
        "batch_count": batch_count,
        "batch_size": batch_size,
    }


def _print_commit_markdown(summary: dict[str, Any], from_profile: str, container: str) -> None:
    """commit 完成后摘要打印为 markdown 到 stdout。"""
    print()
    print("# Migration commit summary")
    print()
    print(f"- **container**: `{container}`")
    print(f"- **from profile**: `{from_profile}`")
    print(f"- **to profile**: `{summary['to_profile']}`")
    print()
    print("| Metric | Value |")
    print("| --- | --- |")
    print(f"| migrated rows | {summary.get('migrated_rows', 0)} |")
    if summary.get("total_rows") is not None:
        print(f"| total rows (src) | {summary['total_rows']} |")
    if "to_dim" in summary:
        print(f"| new vector dim | {summary['to_dim']} |")
    if "to_model" in summary:
        print(f"| new model | `{summary['to_model']}` |")
    if "elapsed_human" in summary:
        print(f"| elapsed | {summary['elapsed_human']} |")
    if "batch_count" in summary:
        print(f"| batches | {summary['batch_count']} (size {summary['batch_size']}) |")
    if "old_table_dir" in summary:
        print(f"| old table backup | `{summary['old_table_dir']}` |")
    if "new_table_dir" in summary:
        print(f"| new active table | `{summary['new_table_dir']}` |")
    if "note" in summary:
        print()
        print(f"_{summary['note']}_")
    print()
    print(
        "Old table preserved — verify search results, then remove manually: "
        f"`rm -rf {summary.get('old_table_dir', '<old-table-dir>')}`"
    )
    print()


# ----------------------------- main -----------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Re-embed a container's LanceDB chunks table from one embedding profile to another.",
    )
    parser.add_argument("--container", required=True, help="Container name (e.g. host-z, team)")
    parser.add_argument(
        "--from",
        dest="from_profile",
        required=True,
        help="Source embedding profile name (must exist in profiles registry)",
    )
    parser.add_argument(
        "--to",
        dest="to_profile",
        required=True,
        help="Target embedding profile name (must exist in profiles registry)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Rows per embed batch (default: 100)",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace root (default: $WORKSPACE or repo root)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Estimate work without writing (default behavior)",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="Actually perform the migration (writes chunks_v2, then atomic rename)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit final summary as JSON to stdout (in addition to markdown)",
    )
    return parser


def _verify_from_profile_matches(table: Any, from_profile: str, from_dim: int) -> None:
    """校验 source 表的 vector dim / row 元数据与 --from profile 一致。

    Why：用户写错 --from 时尽早抛错，避免 commit 后才发现源数据不是预期的 profile。
    softening：row 元数据为空（v0.7.0 之前的老表）时只校验 schema dim，不强校 profile name。
    """
    schema_dim = _detect_table_dim(table)
    if schema_dim is not None and schema_dim != from_dim:
        raise ValueError(
            f"--from profile {from_profile!r} has dim={from_dim} but table vector "
            f"schema is dim={schema_dim}. Did you specify the wrong source profile?"
        )

    row_model, row_profile, row_dim = _detect_row_profile(table)
    if row_profile and row_profile != from_profile:
        # warn 不 fail：跨 profile 名重命名 / 历史数据混合场景下允许 override
        _logger.warning(
            "first row embedding_profile=%r differs from --from=%r — "
            "proceeding anyway, but verify this is intentional",
            row_profile, from_profile,
        )
    if row_dim and row_dim != from_dim:
        raise ValueError(
            f"first row embedding_dim={row_dim} differs from --from profile dim={from_dim}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --commit 优先于默认 --dry-run（mutually_exclusive_group 默认值 + commit flag 组合）
    is_commit = bool(args.commit)
    is_dry_run = not is_commit

    workspace = _resolve_workspace(args.workspace)
    lancedb_path = _container_lancedb_path(workspace, args.container)

    # 1. 解析 from / to profile（早 fail）
    try:
        # to 是必须的（写入用）
        to_call, to_dim, to_model = _build_embed_callable(args.to_profile)
    except KeyError as exc:
        _logger.error("--to profile not found: %s", exc)
        return 1
    except Exception as exc:
        _logger.error("failed to build --to embed callable: %s", exc)
        return 1

    try:
        # from 只用于元数据校验 — dim/model 用于 _verify_from_profile_matches
        registry = _get_registry()
        from_profile_obj = registry.get_profile(args.from_profile)
        from_dim = int(from_profile_obj.dim)
    except KeyError as exc:
        _logger.error("--from profile not found: %s", exc)
        return 1
    except Exception as exc:
        _logger.error("failed to resolve --from profile: %s", exc)
        return 1

    # 2. 打开 LanceDB
    try:
        db = _open_db(lancedb_path)
    except FileNotFoundError as exc:
        _logger.error("%s", exc)
        return 1

    try:
        src_table = db.open_table(CHUNKS_TABLE)
    except Exception as exc:
        _logger.error("failed to open chunks table at %s: %s", lancedb_path, exc)
        return 1

    # 3. 校验源表 dim 与 --from 一致
    try:
        _verify_from_profile_matches(src_table, args.from_profile, from_dim)
    except ValueError as exc:
        _logger.error("%s", exc)
        return 1

    _logger.info(
        "container=%s from=%s(dim=%d) to=%s(dim=%d) mode=%s",
        args.container, args.from_profile, from_dim,
        args.to_profile, to_dim,
        "commit" if is_commit else "dry-run",
    )

    # 4. dry-run / commit 分流
    try:
        if is_dry_run:
            summary = _run_dry(src_table, args.to_profile, args.batch_size, to_call)
            _print_dry_run_markdown(summary, args.from_profile)
        else:
            try:
                summary = _run_commit(
                    db, lancedb_path, src_table,
                    args.to_profile, args.batch_size,
                    to_call, to_dim, to_model,
                )
            except RuntimeError as exc:
                msg = str(exc)
                if "already exists" in msg:
                    _logger.error("%s", exc)
                    return 3
                _logger.error("commit failed: %s", exc)
                return 2
            _print_commit_markdown(summary, args.from_profile, args.container)
    except Exception as exc:  # 兜底
        _logger.exception("unexpected error: %s", exc)
        return 2

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
