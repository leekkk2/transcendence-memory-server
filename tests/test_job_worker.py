"""Tests for the background job worker (scripts/job_worker.py).

We exercise the worker against a real JobQueue but stub the command_resolver
so subprocesses are tiny shell commands (true / false / sleep) instead of the
full LanceDB pipeline.
"""
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
def worker_module():
    sys.modules.pop("scripts.job_worker", None)
    sys.modules.pop("scripts.job_queue", None)
    sys.modules.pop("scripts.server_protection", None)
    sys.modules.pop("job_worker", None)
    sys.modules.pop("job_queue", None)
    sys.modules.pop("server_protection", None)
    return importlib.import_module("scripts.job_worker")


@pytest.fixture
def queue_module():
    sys.modules.pop("scripts.job_queue", None)
    sys.modules.pop("job_queue", None)
    return importlib.import_module("scripts.job_queue")


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_worker_drains_successful_job(queue_module, worker_module, tmp_path):
    queue = queue_module.JobQueue(tmp_path / "q.db")
    job_id = queue.enqueue(op="noop", container="testbox")

    # Resolver returns /usr/bin/true (or `true` on PATH) which exits 0
    def resolver(_job):
        return ["/usr/bin/true"] if Path("/usr/bin/true").exists() else ["true"]

    worker = worker_module.JobWorker(
        queue=queue,
        command_resolver=resolver,
        idle_interval_sec=1,
        between_jobs_sec=1,
        pressure_backoff_sec=1,
        job_timeout_sec=10,
    )
    worker.start()
    try:
        assert _wait_for(lambda: queue.get(job_id).status == "done", timeout=8)
    finally:
        worker.stop(join_timeout=5)
    assert queue.get(job_id).status == "done"


def test_worker_retries_failed_job_with_backoff(queue_module, worker_module, tmp_path, monkeypatch):
    queue = queue_module.JobQueue(tmp_path / "q.db")
    # max_attempts=2 so the second failure transitions to 'failed' permanently
    job_id = queue.enqueue(op="bad", container="testbox", max_attempts=2)

    def resolver(_job):
        return ["/usr/bin/false"] if Path("/usr/bin/false").exists() else ["false"]

    # Compress backoff to 1s for the test
    monkeypatch.setattr(queue_module, "BACKOFF_SCHEDULE", (1, 1, 1, 1, 1))

    worker = worker_module.JobWorker(
        queue=queue,
        command_resolver=resolver,
        idle_interval_sec=1,
        between_jobs_sec=1,
        pressure_backoff_sec=1,
        job_timeout_sec=10,
    )
    worker.start()
    try:
        # Wait for terminal 'failed' state after 2 attempts
        assert _wait_for(lambda: queue.get(job_id).status == "failed", timeout=15)
    finally:
        worker.stop(join_timeout=5)
    job = queue.get(job_id)
    assert job.status == "failed"
    assert job.attempts >= 2


def test_worker_kills_jobs_exceeding_timeout(queue_module, worker_module, tmp_path):
    queue = queue_module.JobQueue(tmp_path / "q.db")
    job_id = queue.enqueue(op="slow", container="testbox", max_attempts=1)

    def resolver(_job):
        # Sleep 30s — will be killed by 1s job timeout
        return ["/bin/sleep", "30"] if Path("/bin/sleep").exists() else ["sleep", "30"]

    worker = worker_module.JobWorker(
        queue=queue,
        command_resolver=resolver,
        idle_interval_sec=1,
        between_jobs_sec=1,
        pressure_backoff_sec=1,
        job_timeout_sec=1,
    )
    worker.start()
    try:
        assert _wait_for(lambda: queue.get(job_id).status in ("failed", "pending"), timeout=8)
    finally:
        worker.stop(join_timeout=5)
    job = queue.get(job_id)
    # max_attempts=1 + first attempt fails on timeout → status should be 'failed'
    assert job.status == "failed"
    assert "timeout" in (job.last_error or "").lower()


def test_worker_skips_when_admit_denied(queue_module, worker_module, tmp_path):
    """Verify worker idles instead of running when admit is refused.

    We replace the GATE attribute on the *worker module's namespace* with a
    stub that always refuses. This works on any platform regardless of whether
    /proc/meminfo is readable.
    """
    queue = queue_module.JobQueue(tmp_path / "q.db")
    job_id = queue.enqueue(op="noop", container="testbox")

    class _RefusingGate:
        @staticmethod
        def check_admit(_snap=None):
            return False, "test forced pressure"

    original_gate = worker_module.GATE
    worker_module.GATE = _RefusingGate()
    try:
        def resolver(_job):
            return ["/usr/bin/true"] if Path("/usr/bin/true").exists() else ["true"]

        worker = worker_module.JobWorker(
            queue=queue,
            command_resolver=resolver,
            idle_interval_sec=1,
            between_jobs_sec=1,
            pressure_backoff_sec=1,
            job_timeout_sec=10,
        )
        worker.start()
        try:
            time.sleep(3)
            assert queue.get(job_id).status == "pending"
        finally:
            worker.stop(join_timeout=5)
    finally:
        worker_module.GATE = original_gate


def test_default_command_resolver_maps_known_ops(queue_module, worker_module, tmp_path):
    resolver = worker_module.default_command_resolver(tmp_path / "scripts")
    job = queue_module.Job(
        id=1, op="embed", container="alpha", payload={},
        status="running", attempts=1, max_attempts=5,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="",
    )
    cmd = resolver(job)
    assert "task_rag_lancedb_ingest.py" in cmd[0]
    assert "--container" in cmd
    assert "alpha" in cmd

    job2 = queue_module.Job(
        id=2, op="ingest-structured", container="beta",
        payload={"input_path": "/tmp/x.json", "doc_type": "memory"},
        status="running", attempts=1, max_attempts=5,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="",
    )
    cmd2 = resolver(job2)
    assert "task_rag_structured_ingest.py" in cmd2[0]
    assert "--input" in cmd2
    assert "/tmp/x.json" in cmd2

    job_unknown = queue_module.Job(
        id=3, op="weird", container="gamma", payload={},
        status="running", attempts=1, max_attempts=5,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="",
    )
    with pytest.raises(ValueError):
        resolver(job_unknown)


def test_default_env_resolver_injects_container(queue_module, worker_module, tmp_path):
    """v0.10.1 regression：默认 env_resolver 必须把 job.container 注入 CONTAINER env，
    否则 worker subprocess 走 default route 而非按 container 路由的 profile。"""
    queue = queue_module.JobQueue(tmp_path / "q.db")
    job = queue_module.Job(
        id=1, op="noop", container="myapp_openai", payload={},
        status="pending", attempts=0, max_attempts=1,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="test",
    )
    worker = worker_module.JobWorker(
        queue=queue,
        command_resolver=lambda _j: ["true"],
    )
    env = worker.env_resolver(job)
    assert env["CONTAINER"] == "myapp_openai", (
        "默认 env_resolver 必须注入 job.container 到 CONTAINER env"
    )


def test_default_env_resolver_handles_empty_container(queue_module, worker_module, tmp_path):
    """job.container 为 None / 空时不应 KeyError，注入空字符串让下游走 default route。"""
    queue = queue_module.JobQueue(tmp_path / "q.db")
    job = queue_module.Job(
        id=1, op="noop", container=None, payload={},
        status="pending", attempts=0, max_attempts=1,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="test",
    )
    worker = worker_module.JobWorker(
        queue=queue,
        command_resolver=lambda _j: ["true"],
    )
    env = worker.env_resolver(job)
    assert env["CONTAINER"] == ""


# ---------- embedding backlog drain ----------


def _force_backlog_due(db_path: Path) -> None:
    """把 backlog 所有行的 next_retry_at 压到过去，让 due_containers 立刻返回。"""
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE embed_backlog SET next_retry_at=0")
        conn.commit()


def test_maybe_drain_backlog_enqueues_due_containers(queue_module, worker_module, tmp_path):
    """到期 backlog 行应被提升为 embed-backlog-retry job（per-container 去重）。"""
    from scripts.embed_backlog import BacklogStore

    db_path = tmp_path / "q.db"
    queue = queue_module.JobQueue(db_path)
    store = BacklogStore(db_path)
    # 同容器两个 chunk + 另一个容器 —— 期望每容器只入一个 job（coalesce）。
    store.record_failure("myapp", "chunk-1", "transient")
    store.record_failure("myapp", "chunk-2", "quota")
    store.record_failure("otherbox", "chunk-9", "timeout")
    _force_backlog_due(db_path)

    worker = worker_module.JobWorker(queue=queue, command_resolver=lambda _j: ["true"])
    worker._maybe_drain_backlog()

    retry_jobs = [j for j in queue.list_jobs(status="pending") if j.op == "embed-backlog-retry"]
    assert len(retry_jobs) == 2
    assert {j.container for j in retry_jobs} == {"myapp", "otherbox"}

    # cadence：间隔内再次调用是 no-op，不会重复入队。
    worker._maybe_drain_backlog()
    retry_jobs2 = [j for j in queue.list_jobs(status="pending") if j.op == "embed-backlog-retry"]
    assert len(retry_jobs2) == 2


def test_maybe_drain_backlog_respects_disable_flag(queue_module, worker_module, tmp_path, monkeypatch):
    """TM_BACKLOG_ENABLED=0 时 drain 完全关闭。"""
    from scripts.embed_backlog import BacklogStore

    monkeypatch.setenv("TM_BACKLOG_ENABLED", "0")
    db_path = tmp_path / "q.db"
    queue = queue_module.JobQueue(db_path)
    store = BacklogStore(db_path)
    store.record_failure("myapp", "chunk-1", "transient")
    _force_backlog_due(db_path)

    worker = worker_module.JobWorker(queue=queue, command_resolver=lambda _j: ["true"])
    worker._maybe_drain_backlog()

    assert not [j for j in queue.list_jobs(status="pending") if j.op == "embed-backlog-retry"]


def test_default_command_resolver_maps_backlog_retry(queue_module, worker_module, tmp_path):
    """embed-backlog-retry op → task_rag_lancedb_ingest.py --mode embed-backlog-retry。"""
    resolver = worker_module.default_command_resolver(tmp_path / "scripts")
    job = queue_module.Job(
        id=1, op="embed-backlog-retry", container="myapp", payload={},
        status="running", attempts=1, max_attempts=5,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="",
    )
    cmd = resolver(job)
    assert "task_rag_lancedb_ingest.py" in cmd[0]
    assert cmd[-2:] == ["--mode", "embed-backlog-retry"]
    assert "--container" in cmd and "myapp" in cmd
