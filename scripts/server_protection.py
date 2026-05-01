"""服务端自我保护层。

设计目标：避免 14:49 复盘事故再现——
当宿主机内存 / IO 处于压力时，重型 ingest 请求继续堆叠会把 swap 撑爆，
进而触发 D-state 进程级联，最终拖死 sshd 与同主机其他业务。

本模块提供四个独立组件，task_rag_server.py 在重型端点前依次咨询：

1. SystemHealthSnapshot / read_system_health()
   读 /proc/meminfo 与 os.getloadavg()，返回不可变快照。
   读取失败（如非 Linux 容器）时退化为 unknown，不阻塞调用。

2. IngestGate
   - 全局信号量限制并发 ingest 数（默认 1）。
   - 每容器锁防止同 container 同时被多个 ingest 子进程写入。
   - check_admit() 综合系统健康判定是否接受新请求；超阈值返回 (False, reason)。

3. RetryRateLimiter
   /embed 在 lancedb SIGABRT (-6/134) 时会自动 retry。但若 SIGABRT 是
   内存压力诱发，retry 会立刻再触发一次 → 自我倍增。这里给每个 container
   加冷却窗口（默认 5 分钟）。

4. BackgroundJobTracker
   subprocess.Popen 启动的后台 ingest 之前完全无追踪，僵尸 PID 会累积，
   且我们无法判断"当前还有多少 bg 在跑"。这里维护 pid → 元数据，定期
   prune 已退出的，提供 count_active 给 IngestGate 决策。
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

# os.kill(pid, 0) is the POSIX liveness-check idiom. On Windows it actually
# terminates the target process (Windows ignores the sig argument), so we
# need a platform-aware probe.
_IS_POSIX = os.name == "posix"

logger = logging.getLogger("transcendence-memory-server.protection")


# ---------- 系统健康快照 ----------


@dataclass(frozen=True)
class SystemHealthSnapshot:
    mem_total_mb: int | None
    mem_available_mb: int | None
    swap_total_mb: int | None
    swap_used_mb: int | None
    load_1min: float | None
    cpu_count: int | None
    # cgroup-scoped values (set when running in a memory-limited container)
    cgroup_mem_limit_mb: int | None = None
    cgroup_mem_current_mb: int | None = None

    @property
    def swap_used_pct(self) -> float | None:
        if not self.swap_total_mb:
            return None
        return round(100.0 * (self.swap_used_mb or 0) / self.swap_total_mb, 1)

    @property
    def load_per_cpu(self) -> float | None:
        if self.load_1min is None or not self.cpu_count:
            return None
        return round(self.load_1min / self.cpu_count, 2)

    @property
    def cgroup_mem_available_mb(self) -> int | None:
        """Bytes the *container* can still allocate before cgroup OOM kills it.

        This is the value `_admit_or_503` should consult inside a Docker
        container — host /proc/meminfo lies about how much memory we actually have.
        """
        if self.cgroup_mem_limit_mb is None or self.cgroup_mem_current_mb is None:
            return None
        return max(0, self.cgroup_mem_limit_mb - self.cgroup_mem_current_mb)

    @property
    def effective_mem_available_mb(self) -> int | None:
        """min(host_available, cgroup_available) — the binding constraint.

        Inside a containerized deploy with a 1.5 GB cgroup limit, host can show
        8 GB free while we're 100 MB from OOM kill. The smaller value wins.
        """
        candidates = [m for m in (self.mem_available_mb, self.cgroup_mem_available_mb) if m is not None]
        return min(candidates) if candidates else None

    def as_dict(self) -> dict:
        return {
            "mem_total_mb": self.mem_total_mb,
            "mem_available_mb": self.mem_available_mb,
            "cgroup_mem_limit_mb": self.cgroup_mem_limit_mb,
            "cgroup_mem_current_mb": self.cgroup_mem_current_mb,
            "cgroup_mem_available_mb": self.cgroup_mem_available_mb,
            "effective_mem_available_mb": self.effective_mem_available_mb,
            "swap_total_mb": self.swap_total_mb,
            "swap_used_mb": self.swap_used_mb,
            "swap_used_pct": self.swap_used_pct,
            "load_1min": self.load_1min,
            "cpu_count": self.cpu_count,
            "load_per_cpu": self.load_per_cpu,
        }


def _read_meminfo() -> dict[str, int]:
    """解析 /proc/meminfo，单位转 KiB → MB。容器/非 Linux 退化为空 dict。"""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out: dict[str, int] = {}
    for line in raw.splitlines():
        # 形如 "MemTotal:       24512680 kB"
        parts = line.split()
        if len(parts) >= 2:
            key = parts[0].rstrip(":")
            try:
                kb = int(parts[1])
                out[key] = kb // 1024  # MB
            except ValueError:
                continue
    return out


def _read_cgroup_int(path: str) -> int | None:
    """Read a single integer from a cgroup pseudo-file. Returns MB, or None.

    Cgroup files are byte counts; we convert to MB for parity with /proc/meminfo
    handling. The literal string "max" (cgroup v2 unbounded) returns None.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None
    if raw == "max" or not raw:
        return None
    try:
        return int(raw) // (1024 * 1024)
    except ValueError:
        return None


def _read_cgroup_memory() -> tuple[int | None, int | None]:
    """Return (limit_mb, current_mb) from cgroup v2 (preferred) or v1 (fallback).

    Outside a memory-limited container both values are None — caller should
    fall back to host /proc/meminfo only.
    """
    # cgroup v2 (unified hierarchy — modern Docker, systemd-managed)
    limit = _read_cgroup_int("/sys/fs/cgroup/memory.max")
    current = _read_cgroup_int("/sys/fs/cgroup/memory.current")
    if limit is not None or current is not None:
        return limit, current
    # cgroup v1 (older kernels / hosts running cgroupfs=legacy)
    limit = _read_cgroup_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    current = _read_cgroup_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    # cgroup v1 reports a sentinel ~9223372036854775807 bytes for "unlimited".
    # That converts to ~8.7 EB; treat anything > 1 PB as unbounded.
    if limit is not None and limit > 1024 * 1024 * 1024:  # > 1 PB in MB
        limit = None
    return limit, current


def read_system_health() -> SystemHealthSnapshot:
    """非阻塞读取系统健康，失败安全（任何字段允许 None）。

    cgroup memory limits 在 1.5 GB 容器里至关重要——host /proc/meminfo 显示
    宿主机视角，会让 admit gate 误判可用内存。
    """
    mem = _read_meminfo()
    try:
        load_1min = os.getloadavg()[0]
    except (OSError, AttributeError):
        load_1min = None
    try:
        cpu_count = os.cpu_count() or None
    except Exception:  # pragma: no cover - defensive
        cpu_count = None
    swap_total = mem.get("SwapTotal")
    swap_free = mem.get("SwapFree")
    swap_used = (swap_total - swap_free) if (swap_total and swap_free is not None) else None
    cgroup_limit, cgroup_current = _read_cgroup_memory()
    return SystemHealthSnapshot(
        mem_total_mb=mem.get("MemTotal"),
        mem_available_mb=mem.get("MemAvailable"),
        swap_total_mb=swap_total,
        swap_used_mb=swap_used,
        load_1min=load_1min,
        cpu_count=cpu_count,
        cgroup_mem_limit_mb=cgroup_limit,
        cgroup_mem_current_mb=cgroup_current,
    )


# ---------- IngestGate ----------


@dataclass
class GateConfig:
    max_concurrent: int = 1
    min_available_mem_mb: int = 800
    max_load_per_cpu: float = 4.0
    max_swap_used_pct: float = 90.0


def _config_from_env() -> GateConfig:
    return GateConfig(
        max_concurrent=int(os.environ.get("TM_MAX_CONCURRENT_INGESTS", "1")),
        min_available_mem_mb=int(os.environ.get("TM_MIN_AVAILABLE_MEM_MB", "800")),
        max_load_per_cpu=float(os.environ.get("TM_MAX_LOAD_PER_CPU", "4.0")),
        max_swap_used_pct=float(os.environ.get("TM_MAX_SWAP_USED_PCT", "90")),
    )


class IngestGate:
    """全局并发限制 + 系统健康预检 + 每容器锁。"""

    def __init__(self, config: GateConfig | None = None):
        self.config = config or _config_from_env()
        self._sem = threading.Semaphore(self.config.max_concurrent)
        self._container_locks: dict[str, threading.Lock] = {}
        self._dict_lock = threading.Lock()

    def _container_lock(self, container: str) -> threading.Lock:
        with self._dict_lock:
            lock = self._container_locks.get(container)
            if lock is None:
                lock = threading.Lock()
                self._container_locks[container] = lock
            return lock

    def check_admit(self, snapshot: SystemHealthSnapshot | None = None) -> tuple[bool, str]:
        """系统健康预检。snapshot 缺省时实时读取。

        若任何关键指标缺失（容器探测不到 /proc/meminfo），不阻塞——失败开放，
        因为我们不希望保护层本身误杀生产请求。

        内存判定优先用 *effective* available（min(host_avail, cgroup_avail)），
        以正确反映容器内的实际可分配内存——host /proc/meminfo 在 1.5 GB 容器
        里会撒谎说还剩 8 GB，而 cgroup 视角才是 OOM kill 的真实门槛。
        """
        snap = snapshot or read_system_health()
        effective_mem = snap.effective_mem_available_mb
        if effective_mem is not None and effective_mem < self.config.min_available_mem_mb:
            scope = "container" if snap.cgroup_mem_available_mb == effective_mem else "host"
            return (
                False,
                f"{scope} memory pressure: available={effective_mem}MB "
                f"< threshold {self.config.min_available_mem_mb}MB",
            )
        if snap.load_per_cpu is not None and snap.load_per_cpu > self.config.max_load_per_cpu:
            return (
                False,
                f"system load high: load_per_cpu={snap.load_per_cpu} "
                f"> threshold {self.config.max_load_per_cpu}",
            )
        if snap.swap_used_pct is not None and snap.swap_used_pct > self.config.max_swap_used_pct:
            return (
                False,
                f"swap pressure: swap_used_pct={snap.swap_used_pct}% "
                f"> threshold {self.config.max_swap_used_pct}%",
            )
        return True, "ok"

    @contextmanager
    def acquire(self, container: str, timeout: float = 0.5) -> Iterator[None]:
        """同步上下文：先抢全局信号量再抢容器锁，任何一步抢不到都 raise。

        timeout 故意短（0.5s）——与其阻塞 HTTP worker，不如直接 503 让客户端退避。
        """
        if not self._sem.acquire(timeout=timeout):
            raise IngestBusyError(
                f"global ingest concurrency limit reached "
                f"(max={self.config.max_concurrent})"
            )
        clock = self._container_lock(container)
        if not clock.acquire(timeout=timeout):
            self._sem.release()
            raise IngestBusyError(
                f"container '{container}' is already being ingested"
            )
        try:
            yield
        finally:
            clock.release()
            self._sem.release()


class IngestBusyError(RuntimeError):
    """所有"重型操作不应进入"的语义错误统一类型。"""


# ---------- RetryRateLimiter ----------


class RetryRateLimiter:
    """每容器重试冷却窗口。

    用 OrderedDict 保留最近 N 个 container 的最后一次重试时间戳，
    超 N 后 LRU 淘汰，避免在多 container 场景下无界增长。
    """

    def __init__(self, cooldown_sec: int | None = None, max_tracked: int = 1024):
        if cooldown_sec is None:
            cooldown_sec = int(os.environ.get("TM_RETRY_COOLDOWN_SEC", "300"))
        self.cooldown_sec = cooldown_sec
        self.max_tracked = max_tracked
        self._last_retry: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def can_retry(self, container: str) -> bool:
        with self._lock:
            ts = self._last_retry.get(container)
            if ts is None:
                return True
            return (time.time() - ts) >= self.cooldown_sec

    def mark_retry(self, container: str) -> None:
        with self._lock:
            self._last_retry[container] = time.time()
            self._last_retry.move_to_end(container)
            while len(self._last_retry) > self.max_tracked:
                self._last_retry.popitem(last=False)


# ---------- BackgroundJobTracker ----------


@dataclass
class BackgroundJob:
    pid: int
    container: str
    started_at: float
    label: str = ""


class BackgroundJobTracker:
    """子进程注册表 + 周期清理。

    register 的进程退出后不会自动从字典移除——必须显式 prune。
    server_protection 的设计原则：subprocess.Popen 调用方在 finish/error 时
    主动调用 unregister，但兜底由后台端点或 prune() 周期清理。
    """

    def __init__(self, max_alive: int = 32):
        self.max_alive = max_alive
        self._jobs: dict[int, BackgroundJob] = {}
        self._lock = threading.Lock()

    def register(self, pid: int, container: str, label: str = "") -> None:
        with self._lock:
            self._jobs[pid] = BackgroundJob(
                pid=pid, container=container, started_at=time.time(), label=label
            )

    def unregister(self, pid: int) -> None:
        with self._lock:
            self._jobs.pop(pid, None)

    def prune(self) -> int:
        """Remove entries for pids that have exited. Returns count removed.

        POSIX uses os.kill(pid, 0) — sig=0 verifies existence without signalling.
        Windows ignores the sig arg and actually calls TerminateProcess, so we
        skip prune on Windows. The container deployment target is Linux, so this
        is a test-environment workaround, not a production concern.
        """
        if not _IS_POSIX:
            # On Windows we have no safe stdlib-only liveness probe. The
            # tracker is best-effort here; subprocess.Popen already detaches
            # so leftover entries get cleaned up on next process restart.
            return 0
        removed = 0
        with self._lock:
            dead: list[int] = []
            for pid in self._jobs.keys():
                try:
                    os.kill(pid, 0)  # send no signal, just check existence
                except ProcessLookupError:
                    dead.append(pid)
                except PermissionError:
                    # process exists but belongs to another user — keep it
                    continue
                except OSError:
                    dead.append(pid)
            for pid in dead:
                self._jobs.pop(pid, None)
                removed += 1
        return removed

    def count_active(self) -> int:
        # prune 是廉价操作（仅 kill -0），每次 count 前清一遍
        self.prune()
        with self._lock:
            return len(self._jobs)

    def list_active(self) -> list[dict]:
        self.prune()
        with self._lock:
            return [
                {
                    "pid": j.pid,
                    "container": j.container,
                    "label": j.label,
                    "running_for_sec": int(time.time() - j.started_at),
                }
                for j in self._jobs.values()
            ]

    def has_capacity(self) -> tuple[bool, str]:
        n = self.count_active()
        if n >= self.max_alive:
            return False, f"too many background jobs alive ({n}/{self.max_alive})"
        return True, "ok"


# ---------- 全局单例（task_rag_server.py 直接 import 使用） ----------


GATE = IngestGate()
RETRY_LIMITER = RetryRateLimiter()
BG_TRACKER = BackgroundJobTracker(
    max_alive=int(os.environ.get("TM_MAX_BG_JOBS", "32"))
)
