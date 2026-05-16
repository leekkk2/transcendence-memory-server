"""Shared fixtures for transcendence-memory-server tests."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


API_KEY = "test-rag-key"
DEFAULT_CONTAINER = "testbox"


def load_server(workspace: Path, monkeypatch, extra_env: dict[str, str] | None = None):
    """Reload the server module with a fresh WORKSPACE and RAG_API_KEY.

    The persistent job worker is disabled by default for tests; tests that
    exercise the queue worker should pass {"TM_DISABLE_WORKER": "0"} explicitly
    via extra_env and then drive the worker manually.
    """
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # 清除缓存的模块以重新加载。
    # server_protection 暴露的全局单例 GATE/RETRY_LIMITER/BG_TRACKER 是模块级状态，
    # 必须一并清掉，否则测试间互相干扰（一个测试 mark_retry 后下一个测试还在冷却中）。
    for mod_name in list(sys.modules):
        if mod_name.startswith("scripts.task_rag_server") or mod_name.startswith("scripts.rag_engine"):
            sys.modules.pop(mod_name, None)
    sys.modules.pop("task_rag_server", None)
    sys.modules.pop("task_rag_server_models", None)
    sys.modules.pop("rag_engine", None)
    sys.modules.pop("arch_detect", None)
    sys.modules.pop("scripts.arch_detect", None)
    sys.modules.pop("server_protection", None)
    sys.modules.pop("scripts.server_protection", None)
    sys.modules.pop("job_queue", None)
    sys.modules.pop("scripts.job_queue", None)
    sys.modules.pop("job_worker", None)
    sys.modules.pop("scripts.job_worker", None)
    sys.modules.pop("raganything_engine", None)
    sys.modules.pop("scripts.raganything_engine", None)
    sys.modules.pop("task_rag_runtime", None)
    sys.modules.pop("scripts.task_rag_runtime", None)
    # v0.7.0：embedding_registry / profiles_loader 有 module-level 单例 _registry。
    # **不清 module**（class identity 不能换，否则 isinstance/test monkeypatch 会失效）；
    # 仅清单例缓存 → 下次 get_registry() 重读 env / yaml。
    if "scripts.embedding_registry" in sys.modules:
        try:
            sys.modules["scripts.embedding_registry"].clear_registry()
        except Exception:  # pragma: no cover
            pass
    elif "embedding_registry" in sys.modules:
        try:
            sys.modules["embedding_registry"].clear_registry()
        except Exception:  # pragma: no cover
            pass

    return importlib.import_module("scripts.task_rag_server")


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(Path(__file__).resolve().parents[1] / "scripts")
    return workspace


@pytest.fixture
def workspace_and_client(tmp_path, monkeypatch):
    """返回 (workspace, TestClient) 元组。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    return workspace, client


def auth_headers(key: str = API_KEY) -> dict[str, str]:
    return {"X-API-KEY": key}
