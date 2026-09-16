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


# os.kill(pid, 0) is the POSIX liveness probe. On Windows it actually
# terminates the target process (Windows ignores sig=0), so the related
# tests are POSIX-only.
posix_only = pytest.mark.skipif(
    os.name != "posix",
    reason="Liveness probing via os.kill(pid, 0) is POSIX-only; Windows would terminate the target.",
)


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


@posix_only
def test_bg_tracker_registers_and_lists(protection):
    tr = protection.BackgroundJobTracker(max_alive=8)
    # 自己进程一定还活着，用它当样本
    tr.register(os.getpid(), container="foo", label="test")
    assert tr.count_active() == 1
    jobs = tr.list_active()
    assert len(jobs) == 1
    assert jobs[0]["container"] == "foo"


@posix_only
def test_bg_tracker_prunes_dead_pids(protection):
    tr = protection.BackgroundJobTracker(max_alive=8)
    # 注册一个肯定不存在的 PID（PID_MAX 通常是 4M+，10M 安全越界）
    tr.register(99999999, container="foo")
    removed = tr.prune()
    assert removed == 1
    assert tr.count_active() == 0


@posix_only
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
    # unregister doesn't require os.kill, safe on all platforms
    tr.register(12345, container="foo")
    tr.unregister(12345)
    # On Windows count_active() short-circuits to current dict size (no prune)
    # On POSIX prune runs but pid 12345 is already removed
    assert tr.count_active() == 0


def test_resident_headroom_with_idle_psi_does_not_block_on_cold_swap(protection):
    snap = _snap(protection, mem_available_mb=12000, swap_total_mb=8000, swap_used_mb=8000)
    object.__setattr__(snap, "memory_psi_some_avg10", 0.0)
    object.__setattr__(snap, "memory_psi_some_avg60", 0.0)
    gate = protection.IngestGate(protection.GateConfig(max_swap_used_pct=95.0))
    assert gate.check_admit(snap)[0]


@pytest.mark.parametrize("available,avg10,avg60", [(1000, 0.0, 0.0), (12000, 2.0, 0.0), (12000, 0.0, 2.0)])
def test_full_swap_still_blocks_low_headroom_or_active_reclaim(protection, available, avg10, avg60):
    snap = _snap(protection, mem_available_mb=available, swap_total_mb=8000, swap_used_mb=8000)
    object.__setattr__(snap, "memory_psi_some_avg10", avg10)
    object.__setattr__(snap, "memory_psi_some_avg60", avg60)
    gate = protection.IngestGate(protection.GateConfig(max_swap_used_pct=95.0))
    assert not gate.check_admit(snap)[0]


def test_cgroup_headroom_is_required_for_cold_swap_exemption(protection):
    snap = _snap(protection, mem_available_mb=12000, swap_total_mb=8000, swap_used_mb=8000,
                 cgroup_mem_limit_mb=4096, cgroup_mem_current_mb=3000)
    object.__setattr__(snap, "memory_psi_some_avg10", 0.0)
    object.__setattr__(snap, "memory_psi_some_avg60", 0.0)
    gate = protection.IngestGate(protection.GateConfig(max_swap_used_pct=95.0))
    assert not gate.check_admit(snap)[0]


def test_memory_psi_reader_parses_linux_stall_measurements(protection, monkeypatch):
    import io
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(
        "some avg10=0.25 avg60=0.50 avg300=1.23 total=1234\nfull avg10=0.00 avg60=0.00 total=567\n"))
    assert protection._read_memory_psi() == (0.25, 0.50)


def test_memory_psi_reader_unavailable_is_conservative(protection, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("PSI unavailable")
    monkeypatch.setattr("builtins.open", unavailable)
    assert protection._read_memory_psi() == (None, None)


def test_native_systemd_reads_own_cgroup(protection, monkeypatch):
    import io
    import builtins
    original = builtins.open
    def opened(path, *args, **kwargs):
        if str(path) == '/proc/self/cgroup':
            return io.StringIO('0::/system.slice/example.service\n')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(builtins, 'open', opened)
    seen=[]
    def read(path):
        seen.append(str(path))
        return 4096 if str(path).endswith('memory.max') else 512
    monkeypatch.setattr(protection, '_read_cgroup_int', read)
    assert protection._read_cgroup_memory()==(4096,512)
    assert seen[0]=='/sys/fs/cgroup/system.slice/example.service/memory.max'
