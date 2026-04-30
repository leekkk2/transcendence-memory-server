"""Tests for the persistent SQLite job queue (scripts/job_queue.py).

Exercises:
- enqueue + coalescing of duplicate (op, container) pairs
- claim_next ordering and atomicity (pending → running)
- mark_done / mark_failed exponential backoff
- crash recovery: 'running' jobs at startup get reset to 'pending'
- cancel only acts on pending
- list / stats / purge
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


@pytest.fixture
def queue_module():
    sys.modules.pop("scripts.job_queue", None)
    sys.modules.pop("job_queue", None)
    return importlib.import_module("scripts.job_queue")


@pytest.fixture
def queue(queue_module, tmp_path):
    return queue_module.JobQueue(tmp_path / "q.db")


# ---------- enqueue ----------


def test_enqueue_returns_increasing_ids(queue):
    a = queue.enqueue(op="embed", container="alpha")
    b = queue.enqueue(op="embed", container="beta")
    assert b > a > 0


def test_coalesce_returns_existing_pending_id(queue):
    a = queue.enqueue(op="embed", container="alpha")
    b = queue.enqueue(op="embed", container="alpha")
    # Same (op, container) while still pending → coalesced
    assert a == b
    stats = queue.stats()
    assert stats["pending"] == 1


def test_coalesce_disabled_creates_duplicates(queue):
    a = queue.enqueue(op="embed", container="alpha")
    b = queue.enqueue(op="embed", container="alpha", coalesce=False)
    assert a != b


def test_coalesce_skips_after_done(queue):
    a = queue.enqueue(op="embed", container="alpha")
    queue.claim_next()
    queue.mark_done(a, result_code=0)
    # Job is done — a new enqueue should create a new id, not reuse the done one
    b = queue.enqueue(op="embed", container="alpha")
    assert b != a


def test_coalesce_advances_next_run_at(queue):
    a = queue.enqueue(op="embed", container="alpha", delay_sec=300)
    job_a = queue.get(a)
    # Re-enqueue with delay=0 should pull next_run_at forward
    queue.enqueue(op="embed", container="alpha", delay_sec=0)
    job_a2 = queue.get(a)
    assert job_a2.next_run_at <= job_a.next_run_at


# ---------- claim_next ----------


def test_claim_returns_oldest_due_first(queue):
    a = queue.enqueue(op="embed", container="alpha")
    time.sleep(0.01)
    b = queue.enqueue(op="embed", container="beta")
    claimed = queue.claim_next()
    assert claimed.id == a
    assert claimed.status == "running"
    assert claimed.attempts == 1


def test_claim_skips_jobs_with_future_next_run_at(queue):
    queue.enqueue(op="embed", container="alpha", delay_sec=3600)
    assert queue.claim_next() is None


def test_claim_returns_none_when_empty(queue):
    assert queue.claim_next() is None


# ---------- mark_done / mark_failed ----------


def test_mark_done_transitions_to_done(queue):
    a = queue.enqueue(op="embed", container="alpha")
    queue.claim_next()
    queue.mark_done(a, result_code=0, note="ok")
    job = queue.get(a)
    assert job.status == "done"
    assert job.result_code == 0


def test_mark_failed_reschedules_with_backoff(queue, queue_module):
    a = queue.enqueue(op="embed", container="alpha", max_attempts=5)
    queue.claim_next()
    queue.mark_failed(a, "boom #1")
    job = queue.get(a)
    assert job.status == "pending"
    # First retry uses BACKOFF_SCHEDULE[0] = 30s
    assert job.next_run_at >= int(time.time()) + 25  # allow some slack
    assert "boom #1" in (job.last_error or "")


def test_mark_failed_terminal_after_max_attempts(queue):
    a = queue.enqueue(op="embed", container="alpha", max_attempts=2)
    queue.claim_next()
    queue.mark_failed(a, "boom #1")
    # Force re-claim by clearing next_run_at
    job = queue.get(a)
    assert job.status == "pending"
    # Manually pull next_run_at to now and claim again
    with queue._conn() as conn:
        conn.execute("UPDATE jobs SET next_run_at=? WHERE id=?", (int(time.time()), a))
    queue.claim_next()
    queue.mark_failed(a, "boom #2")
    job = queue.get(a)
    assert job.status == "failed"


# ---------- recovery ----------


def test_running_jobs_reset_to_pending_on_reload(queue_module, tmp_path):
    db = tmp_path / "q.db"
    q1 = queue_module.JobQueue(db)
    a = q1.enqueue(op="embed", container="alpha")
    q1.claim_next()
    assert q1.get(a).status == "running"
    # Simulate restart: new instance same db
    q2 = queue_module.JobQueue(db)
    job = q2.get(a)
    assert job.status == "pending"
    assert "recovered after restart" in (job.last_error or "")


# ---------- cancel ----------


def test_cancel_pending_succeeds(queue):
    a = queue.enqueue(op="embed", container="alpha")
    assert queue.cancel(a) is True
    job = queue.get(a)
    assert job.status == "cancelled"


def test_cancel_running_fails(queue):
    a = queue.enqueue(op="embed", container="alpha")
    queue.claim_next()
    assert queue.cancel(a) is False
    assert queue.get(a).status == "running"


def test_cancel_unknown_id_returns_false(queue):
    assert queue.cancel(999999) is False


# ---------- list / stats / purge ----------


def test_list_filters_by_status(queue):
    a = queue.enqueue(op="embed", container="alpha")
    queue.enqueue(op="embed", container="beta")
    queue.claim_next()
    queue.mark_done(a)
    pending = queue.list_jobs(status="pending")
    done = queue.list_jobs(status="done")
    assert len(pending) == 1
    assert len(done) == 1


def test_list_filters_by_container(queue):
    queue.enqueue(op="embed", container="alpha")
    queue.enqueue(op="embed", container="beta")
    rows = queue.list_jobs(container="alpha")
    assert len(rows) == 1
    assert rows[0].container == "alpha"


def test_list_invalid_status_raises(queue):
    with pytest.raises(ValueError):
        queue.list_jobs(status="weird")


def test_stats_counts_each_status(queue):
    queue.enqueue(op="embed", container="alpha")
    queue.enqueue(op="embed", container="beta")
    queue.enqueue(op="embed", container="gamma")
    # Claim oldest (alpha) and mark done; the other two stay pending.
    claimed = queue.claim_next()
    queue.mark_done(claimed.id)
    stats = queue.stats()
    assert stats["done"] == 1
    assert stats["pending"] == 2


def test_purge_done_removes_old_finished_jobs(queue):
    a = queue.enqueue(op="embed", container="alpha")
    queue.claim_next()
    queue.mark_done(a)
    # Backdate finished_at so purge picks it up
    with queue._conn() as conn:
        conn.execute("UPDATE jobs SET finished_at=? WHERE id=?", (0, a))
    removed = queue.purge_done(older_than_sec=10)
    assert removed == 1
    assert queue.get(a) is None


# ---------- payload round-trip ----------


def test_payload_persists_through_enqueue_and_get(queue):
    payload = {"input_path": "/tmp/foo.json", "doc_type": "structured_json"}
    a = queue.enqueue(op="ingest-structured", container="alpha", payload=payload)
    job = queue.get(a)
    assert job.payload == payload
