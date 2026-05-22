"""v0.10.2 regression：task_rag_server.child_env 必须支持 container 参数。

背景：v0.10.1 修了 JobWorker subprocess 路径（test_job_worker.py），但 server
端同步 subprocess 调用（task_rag_server.run / run_or_start，影响 /search /embed
/build-manifest 等端点）的 child_env 同名 bug 未修。task_rag_search.py 拿不到
CONTAINER env → embed_text 退化到 default route → 多 profile glob/regex/exact
路由（如 *_openai → openai-small-1024）全部失效，跨 dim 表 search 直接抛
"query dim doesn't match column vector dim" RuntimeError。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def server_module(monkeypatch, tmp_path):
    # 让 import 走 scripts/，与现有 test 文件保持一致
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    # 避免 server 模块 import 触发副作用（profile 加载等）
    if "task_rag_server" in sys.modules:
        del sys.modules["task_rag_server"]
    mod = importlib.import_module("task_rag_server")
    return mod


def test_child_env_injects_container(server_module):
    """v0.10.2 regression：child_env 收到 container 参数 → 注入 CONTAINER env，
    让 subprocess 中的 task_rag_runtime._resolve_chain_for_worker 命中
    'CONTAINER env → registry.resolve(container)' 路径。"""
    env = server_module.child_env(container="myapp_openai")
    assert env.get("CONTAINER") == "myapp_openai"


def test_child_env_omits_container_when_empty(server_module, monkeypatch):
    """container 缺省（空字符串）时不写 CONTAINER env，让 subprocess 走 default route。

    兼容 legacy env-only 部署：旧调用方未传 container，行为应与改造前一致。

    通过 monkeypatch 清掉 os.environ 的 CONTAINER（如 docker-compose 设了），
    严格断言 child_env() / child_env(container='') 不会"自己"引入 CONTAINER key。
    """
    monkeypatch.delenv("CONTAINER", raising=False)

    # 空字符串显式传入
    env_empty = server_module.child_env(container="")
    assert "CONTAINER" not in env_empty, (
        "child_env(container='') 不应注入空字符串 CONTAINER；"
        "下游 _resolve_chain_for_worker 用 os.environ.get('CONTAINER') 拿到 None 才能 fallback 到 default route"
    )

    # 完全省略参数
    env_default = server_module.child_env()
    assert "CONTAINER" not in env_default, (
        "child_env() 默认调用不应自己引入 CONTAINER key"
    )


def test_child_env_container_coexists_with_embedding_override(server_module):
    """同时传 container + embedding_override → 两个 env 都注入。
    runtime 内 override 优先级更高（_resolve_chain_for_worker 先看 override）。"""
    env = server_module.child_env(
        embedding_override="openai-small-1024",
        container="my-container_openai",
    )
    assert env["TM_EMBEDDING_PROFILE_OVERRIDE"] == "openai-small-1024"
    assert env["CONTAINER"] == "my-container_openai"


def test_run_passes_container_to_child_env(server_module, monkeypatch):
    """v0.10.2 regression：run() 应把 container 透传给 child_env，
    避免 subprocess (task_rag_search.py 等) 看不到 CONTAINER env。"""
    captured: dict[str, object] = {}

    def fake_subprocess_run(*args, **kwargs):  # noqa: ANN001
        captured["env"] = kwargs.get("env", {})

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""
        return _FakeCompleted()

    # patch subprocess.run in the server module
    monkeypatch.setattr(server_module.subprocess, "run", fake_subprocess_run)
    # script 必须存在才不会被早 return；用 conftest.py 这种确定存在的 py 文件
    existing = str(Path(__file__).resolve())
    server_module.run([existing], timeout_s=5, container="some_container_openai")

    assert captured["env"]["CONTAINER"] == "some_container_openai", (
        "run() 必须把 container 传给 child_env，否则 subprocess 缺 CONTAINER env"
    )
