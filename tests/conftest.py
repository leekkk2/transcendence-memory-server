"""Shared fixtures for transcendence-memory-server tests."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Ensure repo root is on sys.path at collection time so pure-unit tests can
# `from scripts.X import Y` without first calling load_server() (which does
# the same insertion lazily). scripts/ has no __init__.py — it's a namespace
# package, which only resolves when its parent dir is on sys.path.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


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
    # v0.8.0：reranker_registry 同款单例，同样只清缓存不清 module。
    if "scripts.reranker_registry" in sys.modules:
        try:
            sys.modules["scripts.reranker_registry"].clear_reranker_registry()
        except Exception:  # pragma: no cover
            pass
    elif "reranker_registry" in sys.modules:
        try:
            sys.modules["reranker_registry"].clear_reranker_registry()
        except Exception:  # pragma: no cover
            pass

    return importlib.import_module("scripts.task_rag_server")


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(Path(__file__).resolve().parents[1] / "scripts")
    return workspace


@pytest.fixture(autouse=True)
def _reset_model_breakers():
    """每个测试前后清空全局 circuit breaker 状态。

    模型 fallback 链路（embed / llm / vlm / rerank）共用 model_fallback 里一份
    模块级 ``_breakers`` dict —— 不清会让前一个测试留下的 cooling 在 60s 窗口
    内污染后一个测试（被误跳过 profile → 莫名其妙的 NoUpstreamAvailable）。
    """
    try:
        from scripts.model_fallback import _clear_all_breakers
    except ImportError:  # pragma: no cover - model_fallback 未加载时无需清理
        yield
        return
    _clear_all_breakers()
    yield
    _clear_all_breakers()


@pytest.fixture(autouse=True)
def _reset_profile_registries():
    """每个测试前后清空 embedding / reranker registry 单例缓存 + task_rag_runtime module。

    1. **Registry 单例**：embedding/reranker registry 各 cache 一个 profileSet，
       fixture 仅 monkeypatch.setenv 不会让 cached profile 重新读 env。两个
       registry 模块都没做身份归一，sys.modules 可能同存 `scripts.X` 与 `X`
       两份副本，各持独立 `_registry` 单例 —— 必须双侧都清。

    2. **task_rag_runtime.WS**：module import 时执行
       ``WS = Path(os.environ.get('WORKSPACE', ...))``，路径在 import 时就固化。
       前个 test 用 tmp_path_A 加载 runtime → WS=A；后个 test 设 WORKSPACE=B
       但 lazy import 拿到 cached runtime → 仍 WS=A → lancedb_dir 错路径 →
       in-process /search 报 container 'not_initialized'。pop 掉 sys.modules
       让下次 import 重读 env。
    """
    def _clear():
        for mod_name in ('scripts.embedding_registry', 'embedding_registry'):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, 'clear_registry'):
                try:
                    mod.clear_registry()
                except Exception:  # pragma: no cover
                    pass
        for mod_name in ('scripts.reranker_registry', 'reranker_registry'):
            mod = sys.modules.get(mod_name)
            if mod is not None and hasattr(mod, 'clear_reranker_registry'):
                try:
                    mod.clear_reranker_registry()
                except Exception:  # pragma: no cover
                    pass
        for mod_name in ('scripts.task_rag_runtime', 'task_rag_runtime'):
            sys.modules.pop(mod_name, None)
    _clear()
    yield
    _clear()


@pytest.fixture
def workspace_and_client(tmp_path, monkeypatch):
    """返回 (workspace, TestClient) 元组。"""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    return workspace, client


def auth_headers(key: str = API_KEY) -> dict[str, str]:
    return {"X-API-KEY": key}
