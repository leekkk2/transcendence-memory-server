#!/usr/bin/env python3
"""Cross-container re-embed clone tool — 把一个 container 的 chunks 表克隆到
另一个 container 目录，用指定 embedding profile 重 embed。源 container 完全不动。

适用场景（in-place migrate_embeddings.py 不能覆盖的）：
1. 双轨并运：保留源 container 的 3072 维 + 镜像出 `<X>_openai` 的 1024 维（A/B 对比）
2. 跨 profile 数据迁移测试：不想破坏源数据前先用 clone 验证目标 profile
3. 容器名重命名：clone 到新名 → 验证 → 删除旧名

与 `migrate_embeddings.py` 的差别：
- migrate_embeddings: 同 container in-place 替换（chunks → chunks_v2 → atomic rename，
  旧表保留为 chunks_old_<ts>）。适用：想换 profile 但保持容器名不变。
- clone_container_reembed: 跨 container clone（src 不动，dst 新建）。适用：
  双轨并运 / 实验性试 profile / 重命名。

设计要点：
1. **dry-run 默认**：必须显式 `--commit` 才写盘。dry-run 仅采样一个 batch 实测 latency。
2. **dst 必须不存在 chunks**：避免覆盖。失败 → dst 表残留方便排障，用户手动 rm 重试。
3. **自动 chown**：写入后立即 `os.chown(p, TM_UID, TM_GID)` 把 owner 改成
   tm:tm（uid 10001 / gid 10001），让 server 进程能读（per [[clone-lancedb-chown]] 经验）。
4. **顶级列三字段写入**：每行 vector 替换 + embedding_model / embedding_dim /
   embedding_profile 三字段同步更新（与 task_rag_lancedb_ingest._resolve_embedding_meta 一致）。
5. **复用 server 工具**：通过 migrate_embeddings._build_embed_callable +
   _iter_batches，避免重复实现 embedding fallback 链。

CLI:
    python scripts/clone_container_reembed.py \\
        --src <name> --dst <name> --to-profile <profile> \\
        [--batch-size 50] [--dry-run | --commit] [--workspace /path] \\
        [--no-chown]

Exit codes:
    0 success
    1 配置错（profile 不存在 / src 表不存在）
    2 运行时错（embed 失败 / 写入失败）
    3 状态冲突（dst chunks 已存在）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

import lancedb
import numpy as np

# 让本脚本既能作为 package（pytest）也能直接 `python scripts/clone_container_reembed.py` 跑
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# server 容器内 tm 用户的 UID/GID — 与 Dockerfile / docker-compose TM_RUN_AS_UID 一致
# 容器外宿主跑时也用此值，让宿主上挂载的目录直接对 server uid 可读
TM_UID = 10001
TM_GID = 10001


def _container_lancedb_path(workspace: Path, container: str) -> Path:
    """与 task_rag_runtime.lancedb_dir 同构：tasks/rag/containers/<c>/lancedb"""
    return workspace / "tasks" / "rag" / "containers" / container / "lancedb"


# 系统敏感目录精确匹配黑名单
# 注意：macOS resolve 会把 /etc → /private/etc / /var → /private/var，所以两形式都列
_UNSAFE_WORKSPACE_PATHS_STR = {
    "/",
    "/root", "/home",
    "/etc", "/private/etc",
    "/usr",
    "/var", "/private/var",
    "/bin", "/sbin",
    "/private",
}


def _resolve_workspace(arg_value: str | None) -> Path:
    """workspace 优先级：CLI 参数 > WORKSPACE env > /data（与 task_rag_runtime.WS 兼容）。

    安全检查：拒绝系统敏感根路径（如 / /etc /usr），避免误操作把测试 workspace 指到 fs root。
    匹配方式：path == prefix 拒绝；path 是 prefix 子目录则允许（如 /var/lib/tm-test 合法）。
    """
    if arg_value:
        p = Path(arg_value).expanduser().resolve()
    else:
        env_value = os.environ.get("WORKSPACE")
        if env_value:
            p = Path(env_value).expanduser().resolve()
        else:
            p = Path("/data")

    p_str = str(p)
    if p_str in _UNSAFE_WORKSPACE_PATHS_STR:
        raise SystemExit(
            f"ERR: refusing unsafe workspace path: {p}. Use a dedicated dir (e.g. /data, ~/tm-test)."
        )
    return p


def _detect_table_dim(table: Any) -> int | None:
    """从 LanceDB 表 schema 读 vector 列的固定维度；schema 不含 vector 列时返回 None。"""
    try:
        vec_field = table.schema.field("vector")
        return int(vec_field.type.list_size)
    except Exception:
        return None


def _chown_recursive(path: Path, uid: int, gid: int) -> None:
    """递归 chown，遇错不抛（宿主跑时可能无权 chown，server 容器内有 root 权限）。"""
    try:
        os.chown(path, uid, gid)
    except (PermissionError, OSError):
        return  # 不抛，外层流程不依赖 chown 成功
    for sub in path.rglob("*"):
        try:
            os.chown(sub, uid, gid)
        except (PermissionError, OSError):
            pass


def _run(args: argparse.Namespace) -> int:
    from migrate_embeddings import _build_embed_callable, _iter_batches  # type: ignore

    workspace = _resolve_workspace(args.workspace)
    src_lance = _container_lancedb_path(workspace, args.src)
    dst_lance = _container_lancedb_path(workspace, args.dst)

    if not (src_lance / "chunks.lance").exists():
        print(f"ERR: src container {args.src!r} has no chunks at {src_lance}", file=sys.stderr)
        return 1

    if (dst_lance / "chunks.lance").exists():
        print(
            f"ERR: dst already has chunks at {dst_lance}/chunks.lance — "
            f"remove first if you really want to re-clone (rm -rf that dir)",
            file=sys.stderr,
        )
        return 3

    src_db = lancedb.connect(str(src_lance))
    src_t = src_db.open_table("chunks")
    src_rows = int(src_t.count_rows())
    src_dim = _detect_table_dim(src_t)
    print(f"src:  container={args.src} rows={src_rows} dim={src_dim} path={src_lance}")

    try:
        embed_call, to_dim, to_model = _build_embed_callable(args.to_profile)
    except KeyError as exc:
        print(f"ERR: profile not found: {exc}", file=sys.stderr)
        return 1
    print(f"to:   profile={args.to_profile} model={to_model} dim={to_dim}")

    if src_rows == 0:
        print("src empty — nothing to do")
        return 0

    n_batches = (src_rows + args.batch_size - 1) // args.batch_size

    if args.dry_run:
        first = next(_iter_batches(src_t, args.batch_size))
        _, batch_rows = first
        texts = [str(r.get("text") or "") for r in batch_rows]
        t0 = time.perf_counter()
        vecs = embed_call(texts)
        elapsed = time.perf_counter() - t0
        actual_dim = int(vecs.shape[1]) if hasattr(vecs, "shape") else len(vecs[0])
        print()
        print("DRY-RUN:")
        print(f"  sample: {len(texts)} rows in {elapsed * 1000:.0f} ms (returned dim={actual_dim})")
        if actual_dim != to_dim:
            print(f"  ⚠ profile dim={to_dim} but server returned {actual_dim} — config / gateway mismatch")
        est = elapsed * n_batches
        print(f"  estimated commit: {n_batches} batches × {elapsed:.2f}s = {est:.0f}s (~{est / 60:.1f} min)")
        return 0

    # commit
    dst_lance.mkdir(parents=True, exist_ok=True)
    dst_db = lancedb.connect(str(dst_lance))

    t_start = time.perf_counter()
    migrated = 0
    dst_t = None

    for bi, (start, batch_rows) in enumerate(_iter_batches(src_t, args.batch_size), 1):
        t0 = time.perf_counter()
        texts = [str(r.get("text") or "") for r in batch_rows]
        try:
            vecs = embed_call(texts)
        except Exception as e:
            print(f"ERR: batch {bi}/{n_batches} embed failed at row {start}: {e}", file=sys.stderr)
            return 2
        if len(vecs) != len(batch_rows):
            print(f"ERR: vec count {len(vecs)} != batch size {len(batch_rows)}", file=sys.stderr)
            return 2

        new_rows = []
        for row, vec in zip(batch_rows, vecs):
            new = dict(row)
            new.pop("_distance", None)
            new["vector"] = vec.tolist() if isinstance(vec, np.ndarray) else list(vec)
            new["embedding_model"] = to_model
            new["embedding_dim"] = to_dim
            new["embedding_profile"] = args.to_profile
            # 同步 container 列（被 clone 到新 container 名，便于 search 后端识别归属）
            new["container"] = args.dst
            new_rows.append(new)

        try:
            if dst_t is None:
                dst_t = dst_db.create_table("chunks", data=new_rows, mode="create")
            else:
                dst_t.add(new_rows)
        except Exception as e:
            print(f"ERR: batch {bi}/{n_batches} write failed: {e}", file=sys.stderr)
            return 2

        migrated += len(new_rows)
        ms = (time.perf_counter() - t0) * 1000
        print(
            f"  batch {bi:>3}/{n_batches} +{len(new_rows):>3} rows ({ms:.0f}ms) "
            f"cum={migrated}/{src_rows}",
            flush=True,
        )

    elapsed = time.perf_counter() - t_start
    final = int(dst_t.count_rows()) if dst_t else 0

    # 自动 chown（除非显式 --no-chown）
    if not args.no_chown:
        dst_container_dir = workspace / "tasks" / "rag" / "containers" / args.dst
        _chown_recursive(dst_container_dir, TM_UID, TM_GID)
        print(f"chown: {dst_container_dir} → tm:tm ({TM_UID}/{TM_GID})")

    print()
    print(f"DONE: {final}/{src_rows} rows written to {dst_lance}/chunks.lance")
    print(f"      elapsed={elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"      src untouched: {src_lance}/chunks.lance")
    if final != src_rows:
        print(f"WARN: row count mismatch src={src_rows} dst={final}", file=sys.stderr)
        return 2
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Clone a container's chunks table to a sibling container, re-embedding with a different profile.",
    )
    p.add_argument("--src", required=True, help="Source container name (untouched)")
    p.add_argument("--dst", required=True, help="Destination container name (must not have existing chunks)")
    p.add_argument(
        "--to-profile",
        required=True,
        help="Target embedding profile name (must exist in profiles.yaml)",
    )
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument(
        "--workspace",
        default=None,
        help="Override workspace root (default: $WORKSPACE or /data)",
    )
    p.add_argument(
        "--no-chown",
        action="store_true",
        help="Skip auto chown to tm:tm (10001:10001). Use when running on host without container UID mapping.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Sample one batch to estimate latency; no write")
    mode.add_argument("--commit", action="store_true", help="Actually clone (writes to dst lancedb)")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
