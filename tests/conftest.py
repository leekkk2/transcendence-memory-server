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
    """Reload the server module with a fresh WORKSPACE and RAG_API_KEY."""
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # 清除缓存的模块以重新加载
    for mod_name in list(sys.modules):
        if mod_name.startswith("scripts.task_rag_server") or mod_name.startswith("scripts.rag_engine"):
            sys.modules.pop(mod_name, None)
    sys.modules.pop("task_rag_server", None)
    sys.modules.pop("task_rag_server_models", None)
    sys.modules.pop("rag_engine", None)

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
