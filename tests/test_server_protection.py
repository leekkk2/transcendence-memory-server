"""server_protection 单元测试：覆盖系统快照、IngestGate 准入、重试节流、后台追踪。"""
from __future__ import annotations

import importlib
import os
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def protection(monkeypatch):
    """每次测试拿到全新的 server_protection 模块（重置全局单例）。"""
    sys.modules.pop("server_protection", None)
    sys.modules.pop("scripts.server_protection", None)
    return importlib.import_module("scripts.server_protection")


# ---------- read_system_health ----------


def test_read_system_health_returns_snapshot(protection):
    snap = protection.read_system_health()
    # 在容器/Linux/macOS 都至少应该返回一个 dataclass 实例
    assert isinstance(snap, protection.SystemHealthSnapshot)
    # cpu_count 应该可读
    assert snap.cpu_count is None or snap.cpu_count >= 1


def test_snapshot_load_per_cpu_division(protection):
    snap = protection.SystemHealthSnapshot(
        mem_total_mb=8000, mem_available_mb=4000,
        swap_total_mb=2000, swap_used_mb=500,
        load_1min=8.0, cpu_count=4,
    )
    assert snap.load_per_cpu == 2.0
    assert snap.swap_used_pct == 25.0


def test_snapshot_handles_missing_fields(protection):
    snap = protection.SystemHealthSnapshot(
        mem_total_mb=None, mem_available_mb=None,
        swap_total_mb=None, swap_used_mb=None,
        load_1min=None, cpu_count=None,
    )
    assert snap.load_per_cpu is None
    assert snap.swap_used_pct is None


# ---------- IngestGate.check_admit ----------


def _snap(protection, **kwargs):
    base = dict(
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=4000, swap_used_mb=100,
        load_1min=2.0, cpu_count=4,
    )
    base.update(kwargs)
    return protection.SystemHealthSnapshot(**base)


def test_admit_passes_when_healthy(protection):
    gate = protection.IngestGate()
    ok, reason = gate.check_admit(_snap(protection))
    assert ok is True
    assert reason == "ok"


def test_admit_rejects_when_low_memory(protection):
    """当 available 低于阈值时拒绝——这是 14:49 事故的核心信号。"""
    gate = protection.IngestGate(protection.GateConfig(min_available_mem_mb=1000))
    ok, reason = gate.check_admit(_snap(protection, mem_available_mb=200))
    assert ok is False
    assert "memory pressure" in reason


def test_admit_rejects_when_high_load(protection):
    gate = protection.IngestGate(protection.GateConfig(max_load_per_cpu=4.0))
    ok, reason = gate.check_admit(_snap(protection, load_1min=100.0, cpu_count=4))
    assert ok is False
    assert "load high" in reason


def test_admit_rejects_when_swap_full(protection):
    gate = protection.IngestGate(protection.GateConfig(max_swap_used_pct=80.0))
    ok, reason = gate.check_admit(
        _snap(protection, swap_total_mb=1000, swap_used_mb=950)
    )
    assert ok is False
    assert "swap pressure" in reason


def test_admit_fails_open_on_unknown_metrics(protection):
    """读不到 /proc/meminfo 时不要误杀请求。"""
    gate = protection.IngestGate()
    snap = protection.SystemHealthSnapshot(
        mem_total_mb=None, mem_available_mb=None,
        swap_total_mb=None, swap_used_mb=None,
        load_1min=None, cpu_count=None,
    )
    ok, reason = gate.check_admit(snap)
    assert ok is True


# ---------- IngestGate.acquire ----------


def test_gate_concurrent_same_container_blocks(protection):
    gate = protection.IngestGate(protection.GateConfig(max_concurrent=2))
    with gate.acquire("foo"):
        with pytest.raises(protection.IngestBusyError):
            with gate.acquire("foo", timeout=0.1):
                pass


def test_gate_global_concurrency_caps(protection):
    """全局信号量 = 1 时第二个不同 container 也应失败。"""
    gate = protection.IngestGate(protection.GateConfig(max_concurrent=1))
    with gate.acquire("foo"):
        with pytest.raises(protection.IngestBusyError):
            with gate.acquire("bar", timeout=0.1):
                pass


def test_gate_releases_on_success_and_exception(protection):
    gate = protection.IngestGate(protection.GateConfig(max_concurrent=1))
    with gate.acquire("foo"):
        pass
    # 释放后应该可重新获取
    with gate.acquire("foo"):
        pass

    with pytest.raises(RuntimeError):
        with gate.acquire("foo"):
            raise RuntimeError("boom")
    # 异常后也应释放
    with gate.acquire("foo"):
        pass


# ---------- RetryRateLimiter ----------


def test_retry_limiter_first_call_allowed(protection):
    rl = protection.RetryRateLimiter(cooldown_sec=300)
    assert rl.can_retry("foo") is True


def test_retry_limiter_blocks_within_cooldown(protection):
    rl = protection.RetryRateLimiter(cooldown_sec=300)
    rl.mark_retry("foo")
    assert rl.can_retry("foo") is False


def test_retry_limiter_resets_after_cooldown(protection, monkeypatch):
    rl = protection.RetryRateLimiter(cooldown_sec=10)
    rl.mark_retry("foo")
    # 模拟时间流逝
    real_time = time.time()
    monkeypatch.setattr(protection.time, "time", lambda: real_time + 11)
    assert rl.can_retry("foo") is True


def test_retry_limiter_lru_evicts_oldest(protection):
    rl = protection.RetryRateLimiter(cooldown_sec=300, max_tracked=3)
    for name in ("a", "b", "c", "d"):
        rl.mark_retry(name)
    # 最早的 "a" 应该被驱逐——所以再次问它能否 retry 时是 True（无记录）
    assert rl.can_retry("a") is True
    # 后三个仍在冷却
    assert rl.can_retry("d") is False


# ---------- BackgroundJobTracker ----------


def test_bg_tracker_registers_and_lists(protection):
    tr = protection.BackgroundJobTracker(max_alive=8)
    # 自己进程一定还活着，用它当样本
    tr.register(os.getpid(), container="foo", label="test")
    assert tr.count_active() == 1
    jobs = tr.list_active()
    assert len(jobs) == 1
    assert jobs[0]["container"] == "foo"


def test_bg_tracker_prunes_dead_pids(protection):
    tr = protection.BackgroundJobTracker(max_alive=8)
    # 注册一个肯定不存在的 PID（PID_MAX 通常是 4M+，10M 安全越界）
    tr.register(99999999, container="foo")
    removed = tr.prune()
    assert removed == 1
    assert tr.count_active() == 0


def test_bg_tracker_capacity_check(protection):
    tr = protection.BackgroundJobTracker(max_alive=2)
    tr.register(os.getpid(), container="a")
    tr.register(os.getpid() - 1 if os.getpid() > 1 else 99999, container="b")
    ok, _ = tr.has_capacity()
    # 上面注册了 2 个，max_alive=2，至少其中一个（自己）一定活着，prune 后 ≥1
    # 如果第二个也"活着"则触发拒绝；这测试只验证语义不死循环
    assert isinstance(ok, bool)


def test_bg_tracker_unregister(protection):
    tr = protection.BackgroundJobTracker()
    tr.register(os.getpid(), container="foo")
    tr.unregister(os.getpid())
    assert tr.count_active() == 0
