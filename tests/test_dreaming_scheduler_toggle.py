"""DR4 单测：调度器热开关 —— stop_scheduler 幂等 + config_updated hook 启停。

覆盖：

  * stop_scheduler 幂等（无调度器 / 重复调用 / shutdown 抛错都不 raise）；
  * config_store.register_update_hook 去重注册、set()/refresh() 两路触发、
    hook 抛错被吞（不破坏 config 写路径）；
  * server 侧 _dreaming_scheduler_hook：scheduler_enabled true→start /
    false→stop / 无关 key 不动作；
  * lifespan 启动即注册 hook（TestClient 驱动）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import load_server, make_workspace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config_store  # noqa: E402
import dreaming  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()
    dreaming.reset_for_tests()
    yield
    config_store.reset_for_tests()
    dreaming.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


class FakeScheduler:
    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self, wait: bool = True) -> None:
        self.shutdowns += 1


# ── stop_scheduler 幂等 ──────────────────────────────────────────────────────


def test_stop_scheduler_noop_when_not_running():
    _run(dreaming.stop_scheduler())
    _run(dreaming.stop_scheduler())  # 重复调用同样安全
    assert dreaming._scheduler_running() is False


def test_stop_scheduler_shuts_down_once(monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(dreaming, "_SCHEDULER", fake)
    _run(dreaming.stop_scheduler())
    assert fake.shutdowns == 1
    assert dreaming._scheduler_running() is False
    _run(dreaming.stop_scheduler())
    assert fake.shutdowns == 1  # 幂等：第二次不再 shutdown


def test_stop_scheduler_swallows_shutdown_error(monkeypatch):
    class Exploding:
        def shutdown(self, wait: bool = True) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(dreaming, "_SCHEDULER", Exploding())
    _run(dreaming.stop_scheduler())  # must not raise
    assert dreaming._scheduler_running() is False


# ── config_store hook 派发 ───────────────────────────────────────────────────


def test_register_update_hook_dedupes_and_fires_on_set():
    fired: list[list[str]] = []

    async def hook(keys):
        fired.append(keys)

    config_store.register_update_hook(hook)
    config_store.register_update_hook(hook)  # 重复注册去重
    ok = _run(config_store.set("config:dreaming:scheduler_enabled", True))
    assert ok is True
    assert fired == [["config:dreaming:scheduler_enabled"]]


def test_refresh_fires_hooks():
    fired: list[list[str]] = []

    async def hook(keys):
        fired.append(keys)

    config_store.register_update_hook(hook)
    _run(config_store.refresh(["config:dreaming:scheduler_enabled"]))
    assert fired == [["config:dreaming:scheduler_enabled"]]


def test_hook_failure_swallowed_set_still_succeeds():
    async def bad_hook(keys):
        raise RuntimeError("hook exploded")

    config_store.register_update_hook(bad_hook)
    assert _run(config_store.set("config:dreaming:scheduler_enabled", False)) is True


# ── server 侧热开关 hook ─────────────────────────────────────────────────────


def _toggle_recorders(server, monkeypatch):
    calls: list[str] = []

    async def _start():
        calls.append("start")
        return True

    async def _stop():
        calls.append("stop")

    monkeypatch.setattr(server.dreaming, "start_scheduler", _start)
    monkeypatch.setattr(server.dreaming, "stop_scheduler", _stop)
    return calls


def test_server_hook_starts_on_enable(tmp_path, monkeypatch):
    server = load_server(make_workspace(tmp_path), monkeypatch)
    calls = _toggle_recorders(server, monkeypatch)
    monkeypatch.setattr(
        server.config_store, "get_cached",
        lambda key, default=None: True
        if key == "config:dreaming:scheduler_enabled" else default,
    )
    _run(server._dreaming_scheduler_hook(["config:dreaming:scheduler_enabled"]))
    assert calls == ["start"]


def test_server_hook_stops_on_disable(tmp_path, monkeypatch):
    server = load_server(make_workspace(tmp_path), monkeypatch)
    calls = _toggle_recorders(server, monkeypatch)
    monkeypatch.setattr(
        server.config_store, "get_cached",
        lambda key, default=None: False
        if key == "config:dreaming:scheduler_enabled" else default,
    )
    _run(server._dreaming_scheduler_hook(["config:dreaming:scheduler_enabled"]))
    assert calls == ["stop"]


def test_server_hook_ignores_unrelated_keys(tmp_path, monkeypatch):
    server = load_server(make_workspace(tmp_path), monkeypatch)
    calls = _toggle_recorders(server, monkeypatch)
    _run(server._dreaming_scheduler_hook(["config:dreaming:trigger_cron"]))
    assert calls == []


def test_lifespan_registers_hook(tmp_path, monkeypatch):
    server = load_server(make_workspace(tmp_path), monkeypatch)
    with TestClient(server.app):
        assert server._dreaming_scheduler_hook in server.config_store._UPDATE_HOOKS


def test_put_scheduler_enabled_hot_toggles_via_config_set(tmp_path, monkeypatch):
    # 端到端（不经 Redis）：set() → hook → start/stop 立即生效
    server = load_server(make_workspace(tmp_path), monkeypatch)
    calls = _toggle_recorders(server, monkeypatch)
    server.config_store.register_update_hook(server._dreaming_scheduler_hook)
    assert _run(server.config_store.set("config:dreaming:scheduler_enabled", True))
    assert _run(server.config_store.set("config:dreaming:scheduler_enabled", False))
    assert calls == ["start", "stop"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
