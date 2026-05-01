"""v0.6.1 audit-fix tests.

Covers:
1. cgroup memory reading and effective_mem_available_mb
2. JobQueue.enqueue(max_pending=...) → QueueFullError
3. Crash-recovery attempts++ + transition to 'failed' once exceeded
4. purge_done now removes 'failed' rows too
5. _check_memory_objects_size on oversized JSONL
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------- cgroup memory reading ----------


@pytest.fixture
def fresh_protection():
    sys.modules.pop("server_protection", None)
    sys.modules.pop("scripts.server_protection", None)
    return importlib.import_module("scripts.server_protection")


def test_cgroup_v2_limit_and_current_are_read(fresh_protection, tmp_path, monkeypatch):
    """cgroup v2 path reads memory.max + memory.current and converts to MB."""
    fake_root = tmp_path / "cgroup"
    fake_root.mkdir()
    (fake_root / "memory.max").write_text(str(1500 * 1024 * 1024))  # 1500 MB
    (fake_root / "memory.current").write_text(str(800 * 1024 * 1024))  # 800 MB

    real_open = open

    def patched_open(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup/"):
            mapped = fake_root / Path(str(path)).name
            if mapped.exists():
                return real_open(mapped, *args, **kwargs)
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched_open)
    limit, current = fresh_protection._read_cgroup_memory()
    assert limit == 1500
    assert current == 800


def test_cgroup_v2_max_treated_as_unbounded(fresh_protection, tmp_path, monkeypatch):
    fake_root = tmp_path / "cgroup"
    fake_root.mkdir()
    (fake_root / "memory.max").write_text("max")
    (fake_root / "memory.current").write_text(str(100 * 1024 * 1024))

    real_open = open

    def patched_open(path, *args, **kwargs):
        if str(path).startswith("/sys/fs/cgroup/"):
            mapped = fake_root / Path(str(path)).name
            if mapped.exists():
                return real_open(mapped, *args, **kwargs)
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", patched_open)
    limit, current = fresh_protection._read_cgroup_memory()
    assert limit is None
    assert current == 100


def test_effective_mem_takes_min_of_host_and_cgroup(fresh_protection):
    snap = fresh_protection.SystemHealthSnapshot(
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=100,
        load_1min=1.0, cpu_count=4,
        cgroup_mem_limit_mb=1500, cgroup_mem_current_mb=1400,
    )
    # host says 8000 free, cgroup says only 100 free → effective = 100
    assert snap.cgroup_mem_available_mb == 100
    assert snap.effective_mem_available_mb == 100


def test_effective_mem_falls_back_to_host_when_no_cgroup(fresh_protection):
    snap = fresh_protection.SystemHealthSnapshot(
        mem_total_mb=16000, mem_available_mb=4000,
        swap_total_mb=None, swap_used_mb=None,
        load_1min=1.0, cpu_count=4,
        cgroup_mem_limit_mb=None, cgroup_mem_current_mb=None,
    )
    assert snap.cgroup_mem_available_mb is None
    assert snap.effective_mem_available_mb == 4000


def test_admit_rejects_using_effective_mem(fresh_protection):
    """The admit gate should look at the binding constraint, not host alone."""
    gate = fresh_protection.IngestGate(
        fresh_protection.GateConfig(min_available_mem_mb=500)
    )
    # Host pretends to have 8 GB free, container is 100 MB from OOM.
    snap = fresh_protection.SystemHealthSnapshot(
        mem_total_mb=16000, mem_available_mb=8000,
        swap_total_mb=2000, swap_used_mb=100,
        load_1min=1.0, cpu_count=4,
        cgroup_mem_limit_mb=1500, cgroup_mem_current_mb=1400,
    )
    ok, reason = gate.check_admit(snap)
    assert ok is False
    assert "container memory pressure" in reason


# ---------- JobQueue.enqueue back-pressure ----------


@pytest.fixture
def queue(tmp_path):
    sys.modules.pop("job_queue", None)
    sys.modules.pop("scripts.job_queue", None)
    mod = importlib.import_module("scripts.job_queue")
    return mod.JobQueue(tmp_path / "q.db")


def test_enqueue_raises_queue_full_when_max_pending_exceeded(queue):
    # Resolve the QueueFullError class via the same module instance the
    # `queue` fixture used — avoid re-importing, which would create a
    # second class object that pytest.raises wouldn't match.
    jq = sys.modules["scripts.job_queue"]

    for i in range(3):
        queue.enqueue(op="embed", container=f"c{i}", coalesce=False)

    with pytest.raises(jq.QueueFullError) as excinfo:
        queue.enqueue(op="embed", container="c4", max_pending=3)
    assert "queue saturated" in str(excinfo.value)


def test_enqueue_max_pending_allows_room(queue):
    queue.enqueue(op="embed", container="a", max_pending=10)
    queue.enqueue(op="embed", container="b", max_pending=10)
    queue.enqueue(op="embed", container="c", max_pending=10)
    # 3 < 10 — should accept the next one fine.
    job_id = queue.enqueue(op="embed", container="d", max_pending=10)
    assert job_id > 0


# ---------- Crash recovery attempts++ + permanent failed ----------


def test_crash_recovery_increments_attempts(tmp_path):
    """A 'running' job at startup gets attempts+1 and stays pending until cap."""
    sys.modules.pop("scripts.job_queue", None)
    jq_mod = importlib.import_module("scripts.job_queue")
    db = tmp_path / "queue.db"

    # First boot: claim a job (attempts = 1, status='running').
    q1 = jq_mod.JobQueue(db)
    q1.enqueue(op="embed", container="x", max_attempts=3)
    job = q1.claim_next()
    assert job is not None and job.attempts == 1 and job.status == "running"

    # Simulate restart — JobQueue() constructor runs crash recovery.
    q2 = jq_mod.JobQueue(db)
    recovered = q2.get(job.id)
    assert recovered is not None
    assert recovered.attempts == 2  # was 1, +1 by recovery
    assert recovered.status == "pending"


def test_crash_recovery_marks_poison_job_failed_at_cap(tmp_path):
    """Once attempts >= max_attempts the recovery transitions to permanent failed."""
    sys.modules.pop("scripts.job_queue", None)
    jq_mod = importlib.import_module("scripts.job_queue")
    db = tmp_path / "queue.db"

    q1 = jq_mod.JobQueue(db)
    job_id = q1.enqueue(op="embed", container="x", max_attempts=2)
    # claim once → attempts=1, status='running'; that already pushes it to 1.
    j = q1.claim_next()
    assert j is not None and j.attempts == 1

    # Restart: recovery bumps attempts to 2; 2 >= max_attempts → 'failed'.
    q2 = jq_mod.JobQueue(db)
    recovered = q2.get(job_id)
    assert recovered is not None
    assert recovered.attempts == 2
    assert recovered.status == "failed"  # was 'running', recovery says: cap hit
    assert "recovered after restart" in (recovered.last_error or "")


# ---------- purge_done now also removes failed rows ----------


def test_purge_done_removes_failed_rows(queue):
    # Enqueue then mark as failed (force terminal state with finished_at in past).
    job_id = queue.enqueue(op="embed", container="x", max_attempts=1)
    j = queue.claim_next()
    assert j is not None
    # Force the job to fail terminally.
    queue.mark_failed(job_id, "boom")  # attempts=1, max=1 → status='failed'

    # Pretend the failed row is old (24h+ ago).
    with queue._conn() as conn:
        conn.execute(
            "UPDATE jobs SET finished_at=? WHERE id=?",
            (int(time.time()) - 30 * 86400, job_id),
        )

    removed = queue.purge_done(older_than_sec=7 * 86400)
    assert removed == 1
    assert queue.get(job_id) is None


# ---------- _check_memory_objects_size ----------


def test_check_memory_objects_size_below_limit_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "test")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test")
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    sys.modules.pop("scripts.task_rag_server", None)
    sys.modules.pop("task_rag_server", None)
    server = importlib.import_module("scripts.task_rag_server")

    small = tmp_path / "small.jsonl"
    small.write_text('{"id":"a"}\n')
    # Should not raise.
    server._check_memory_objects_size(small, op="test")


def test_check_memory_objects_size_over_limit_raises_507(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "test")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test")
    monkeypatch.setenv("TM_DISABLE_WORKER", "1")
    monkeypatch.setenv("TM_MEMORY_OBJECTS_MAX_BYTES", "100")
    sys.modules.pop("scripts.task_rag_server", None)
    sys.modules.pop("task_rag_server", None)
    server = importlib.import_module("scripts.task_rag_server")

    big = tmp_path / "big.jsonl"
    big.write_bytes(b"x" * 500)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        server._check_memory_objects_size(big, op="test")
    assert excinfo.value.status_code == 507
    detail = excinfo.value.detail
    assert detail["error"] == "memory_objects_too_large"
    assert detail["size_bytes"] == 500
