"""BacklogStore 单测：与 JobQueue 同库共存、分级退避、dead-letter。

BacklogStore 本身由基础文件提供（基础模块，本测试不改它），这里从消费者视角验证其与
JobQueue 在同一个 queue.db 上互不干扰，以及 backlog 重试调度的关键不变量。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from embed_backlog import BacklogStore, next_retry_delay  # noqa: E402
from job_queue import JobQueue  # noqa: E402

FUTURE = 9_999_999_999  # 让所有退避到期的 claim 时刻


def test_backlog_and_jobqueue_coexist_on_same_db(tmp_path):
    """backlog 与 job 队列共用 queue.db（单一 WAL / 崩溃恢复域），互不干扰。"""
    db = tmp_path / "queue.db"
    jq = JobQueue(db)
    bl = BacklogStore(db)

    job_id = jq.enqueue("embed", "testbox")
    assert job_id > 0
    bl.record_failure("testbox", "chunk-1", "quota", content_hash="h1")

    # 两张表各自的数据都在，互不覆盖
    assert jq.get(job_id) is not None
    items = bl.list_items("testbox")
    assert len(items) == 1 and items[0].chunk_id == "chunk-1"

    # 新建实例重新打开同库仍可见双方数据
    assert JobQueue(db).get(job_id) is not None
    assert len(BacklogStore(db).list_items("testbox")) == 1


def test_claim_resolve_roundtrip(tmp_path):
    bl = BacklogStore(tmp_path / "queue.db")
    bl.record_failure("testbox", "c1", "transient", content_hash="h1")

    due = bl.claim_due("testbox", 50, now=FUTURE)
    assert [it.chunk_id for it in due] == ["c1"]
    # claim 后该行转 retrying
    assert bl.counts("testbox")["retrying"] == 1
    # 重试成功 → resolved
    assert bl.mark_resolved_many("testbox", ["c1"]) == 1
    counts = bl.counts("testbox")
    assert counts["resolved"] == 1 and counts["retrying"] == 0
    # resolved 行不再被 claim
    assert bl.claim_due("testbox", 50, now=FUTURE) == []


def test_retry_failure_reschedules_with_backoff(tmp_path):
    """重试再失败 → attempts+1、重新排到 waiting、退避变长。"""
    bl = BacklogStore(tmp_path / "queue.db")
    bl.record_failure("testbox", "c1", "quota", content_hash="h1")
    first = bl.list_items("testbox")[0]
    assert first.attempts == 1 and first.status == "waiting"

    bl.claim_due("testbox", 50, now=FUTURE)
    bl.record_failure("testbox", "c1", "quota", content_hash="h1")
    second = bl.list_items("testbox")[0]
    assert second.attempts == 2
    assert second.status == "waiting"
    assert second.next_retry_at >= first.next_retry_at


def test_next_retry_delay_grows_and_respects_retry_after():
    # 退避随 attempts 增长（quota schedule 分钟 → 天）
    d0 = next_retry_delay("quota", 0)
    d4 = next_retry_delay("quota", 4)
    assert d4 > d0
    # 上游 Retry-After 作为下界
    assert next_retry_delay("quota", 0, retry_after=88_888) >= 88_888
    # 超出退避表长度 → 停在最后一档，不报错
    assert next_retry_delay("quota", 999) > 0


def test_permanent_error_becomes_dead_letter(tmp_path):
    """permanent 错误直接进 dead，不被 claim，purge 永不清掉。"""
    bl = BacklogStore(tmp_path / "queue.db")
    bl.record_failure("testbox", "bad-chunk", "permanent", content_hash="h1")

    dead = bl.list_items("testbox", status="dead")
    assert len(dead) == 1 and dead[0].status == "dead"
    # dead 不进重试队列
    assert bl.claim_due("testbox", 50, now=FUTURE) == []
    # purge_resolved 只清 resolved，dead 永久保留作 dead-letter
    bl.purge_resolved(older_than_sec=0)
    assert len(bl.list_items("testbox", status="dead")) == 1


def test_restart_recovers_retrying_rows(tmp_path):
    """崩溃恢复：重启时残留 retrying 行回 waiting，让下次 drain 重新拾取。"""
    db = tmp_path / "queue.db"
    bl = BacklogStore(db)
    bl.record_failure("testbox", "c1", "transient", content_hash="h1")
    bl.claim_due("testbox", 50, now=FUTURE)
    assert bl.counts("testbox")["retrying"] == 1

    # 模拟进程重启：新建实例触发 _init_schema 的 retrying → waiting 恢复
    recovered = BacklogStore(db)
    assert recovered.counts("testbox")["retrying"] == 0
    assert recovered.counts("testbox")["waiting"] == 1


def test_summary_reports_quota_blocked_signal(tmp_path):
    bl = BacklogStore(tmp_path / "queue.db")
    bl.record_failure("testbox", "c1", "quota", content_hash="h1")
    summary = bl.summary("testbox")
    assert summary["active"] == 1
    assert summary["last_error_class"] == "quota"
    assert summary["next_retry_at"] is not None
