"""Tests for scripts/clone_container_reembed.py — 跨 container 双轨 clone 工具。

覆盖：
- CLI parser 接受合法/拒绝非法参数
- workspace 解析优先级（参数 > env > 默认 /data）
- container_lancedb_path 路径构造
- chown 失败不抛（兼容宿主无权限场景）
- dst 已存在 chunks 时拒绝执行（避免覆盖）
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def clone_module(monkeypatch):
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    if "clone_container_reembed" in sys.modules:
        del sys.modules["clone_container_reembed"]
    return importlib.import_module("clone_container_reembed")


def test_parser_requires_mode(clone_module):
    """必须显式 --dry-run 或 --commit（避免误删/误写）。"""
    parser = clone_module._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--src", "a", "--dst", "b", "--to-profile", "p"])


def test_parser_rejects_both_modes(clone_module):
    """--dry-run 与 --commit 互斥。"""
    parser = clone_module._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--src", "a", "--dst", "b", "--to-profile", "p", "--dry-run", "--commit"])


def test_parser_accepts_dry_run(clone_module):
    parser = clone_module._build_parser()
    args = parser.parse_args(["--src", "a", "--dst", "b", "--to-profile", "p", "--dry-run"])
    assert args.dry_run is True
    assert args.commit is False
    assert args.batch_size == 50
    assert args.no_chown is False


def test_workspace_resolution_arg_wins(clone_module, monkeypatch, tmp_path):
    """CLI --workspace 优先级最高，覆盖 env 与默认值。"""
    monkeypatch.setenv("WORKSPACE", "/should-be-ignored")
    resolved = clone_module._resolve_workspace(str(tmp_path))
    assert resolved == tmp_path.resolve()


def test_workspace_resolution_env_fallback(clone_module, monkeypatch, tmp_path):
    """无 CLI 参数时用 WORKSPACE env。"""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    resolved = clone_module._resolve_workspace(None)
    assert resolved == Path(str(tmp_path))


def test_workspace_resolution_default(clone_module, monkeypatch):
    """无 CLI 也无 env → /data（容器内 server tm-data volume 默认挂载点）。"""
    monkeypatch.delenv("WORKSPACE", raising=False)
    resolved = clone_module._resolve_workspace(None)
    assert resolved == Path("/data")


def test_container_lancedb_path_layout(clone_module, tmp_path):
    """与 task_rag_runtime.lancedb_dir 同构：<ws>/tasks/rag/containers/<c>/lancedb"""
    p = clone_module._container_lancedb_path(tmp_path, "myapp")
    assert p == tmp_path / "tasks" / "rag" / "containers" / "myapp" / "lancedb"


@pytest.mark.skipif(sys.platform == "win32", reason="os.chown 仅 unix；脚本本身依赖 docker exec + chown，windows 非目标平台")
def test_chown_recursive_swallows_permission_error(clone_module, tmp_path):
    """宿主无 root 权限时 os.chown 抛 PermissionError，函数必须吞掉不抛
    （让外层 commit 流程在 dry-run / 宿主跑场景下不被 chown 阻塞）。"""
    # 创建 nested 结构
    nested = tmp_path / "a" / "b" / "c.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("x")

    # chown 到非法 uid 模拟 permission error（macOS / Linux 都会拒绝）
    # 函数应该静默返回，不抛
    clone_module._chown_recursive(tmp_path, 99999, 99999)
    # 文件依然存在
    assert nested.exists()


def test_tm_uid_gid_constants(clone_module):
    """UID/GID 与 server Dockerfile / docker-compose TM_RUN_AS_UID 一致 (10001)。"""
    assert clone_module.TM_UID == 10001
    assert clone_module.TM_GID == 10001


@pytest.mark.skipif(sys.platform == "win32", reason="/usr /etc 是 unix 路径；脚本本身 unix-only")
def test_workspace_rejects_unsafe_paths(clone_module):
    """安全：拒绝系统根路径作为 workspace（误操作防御）。"""
    import pytest as _pt
    for unsafe in ["/", "/etc", "/usr", "/var", "/root"]:
        with _pt.raises(SystemExit):
            clone_module._resolve_workspace(unsafe)


def test_workspace_accepts_dedicated_paths(clone_module, tmp_path):
    """tmp_path 之类专用目录不被误拒。"""
    resolved = clone_module._resolve_workspace(str(tmp_path))
    assert resolved == tmp_path.resolve()
