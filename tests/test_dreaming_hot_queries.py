"""DR2 单测：/search 查询频度计数 → 阈值筛候选 → dream:cache:hot 静态化全链路。

不依赖真 Redis —— 用 FakeRedis 替身 monkeypatch `redis_client.get_client`，
redis_client 的 hgetall / cfg_set 等 helper 都经由该入口拿 client，链路即闭合。
覆盖：

  * record_query_frequency 的规范化（strip+lower+压缩空白）/ 空查询 no-op /
    Redis down 静默跳过 / 48h TTL；
  * cache_threshold 真读配置（config_store.get_cached）与 >= 边界；
  * dry_run 只报告（candidate_queries 摘要+count，零写入）；
  * 真实 run 写 dream:cache:hot:<hash>（24h TTL，值含 query/count/date）；
  * run_dream_cycle 全链路下 summary 不再恒 no_data（no_candidates 带 scanned）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("TM_REDIS_ENABLED", "0")

import config_store  # noqa: E402
import dreaming  # noqa: E402


class FakeRedis:
    """最小 redis.asyncio 替身：仅实现本链路用到的命令，全部进程内存。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, object]] = {}
        self.strings: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = int(bucket.get(field, 0)) + amount
        return int(bucket[field])

    async def hset(self, key: str, field: str, value: str) -> int:
        self.hashes.setdefault(key, {})[field] = value
        return 1

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def hgetall(self, key: str) -> dict[str, object]:
        return dict(self.hashes.get(key, {}))

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.strings[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str):
        return self.strings.get(key)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()
    dreaming.reset_for_tests()
    yield
    config_store.reset_for_tests()
    dreaming.reset_for_tests()


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def _get_client():
        return fake

    monkeypatch.setattr(dreaming.redis_client, "get_client", _get_client)
    return fake


def _run(coro):
    return asyncio.run(coro)


def _fake_cfg(table):
    def _get(key, default=None):
        return table.get(key, default)

    return _get


def _day() -> str:
    return dreaming._utc_day()


def _freq_key() -> str:
    return f"usage:queryfreq:daily:{_day()}"


def _text_key() -> str:
    return f"usage:queryfreq:text:{_day()}"


# ── record_query_frequency ───────────────────────────────────────────────────


def test_record_normalizes_and_counts(fake_redis):
    _run(dreaming.record_query_frequency("  Hello   World "))
    _run(dreaming.record_query_frequency("hello world"))
    field = dreaming.query_hash("hello world")
    assert fake_redis.hashes[_freq_key()][field] == 2
    assert fake_redis.hashes[_text_key()][field] == "hello world"
    # 48h TTL 钉在计数与原文映射两个 key 上
    assert fake_redis.ttls[_freq_key()] == 48 * 3600
    assert fake_redis.ttls[_text_key()] == 48 * 3600


def test_record_truncates_text_to_120(fake_redis):
    long_query = "q" * 500
    _run(dreaming.record_query_frequency(long_query))
    field = dreaming.query_hash(long_query)
    assert fake_redis.hashes[_text_key()][field] == "q" * 120


def test_record_empty_query_is_noop(fake_redis):
    _run(dreaming.record_query_frequency("   "))
    assert fake_redis.hashes == {}


def test_record_redis_down_is_silent(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(dreaming.redis_client, "get_client", _none)
    _run(dreaming.record_query_frequency("anything"))  # must not raise


def test_record_redis_error_is_swallowed(fake_redis, monkeypatch):
    async def _boom(*a, **kw):
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(fake_redis, "hincrby", _boom)
    _run(dreaming.record_query_frequency("anything"))  # must not raise


# ── threshold（真读配置） + 边界 ─────────────────────────────────────────────


def test_threshold_read_from_config_with_boundary(fake_redis, monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:cache_threshold": 3,
    }))
    for _ in range(3):
        _run(dreaming.record_query_frequency("hot query"))
    for _ in range(2):
        _run(dreaming.record_query_frequency("warm query"))
    scanned, candidates = _run(dreaming._hot_query_candidates(_day()))
    assert scanned == 2
    # count == threshold 入选；count == threshold-1 出局
    assert [c["query"] for c in candidates] == ["hot query"]
    assert candidates[0]["count"] == 3


def test_threshold_default_is_10(fake_redis):
    # 无配置覆盖 → get_cached 回落 caller default 10
    for _ in range(9):
        _run(dreaming.record_query_frequency("nine times"))
    _, candidates = _run(dreaming._hot_query_candidates(_day()))
    assert candidates == []
    _run(dreaming.record_query_frequency("nine times"))
    _, candidates = _run(dreaming._hot_query_candidates(_day()))
    assert len(candidates) == 1 and candidates[0]["count"] == 10


# ── consolidate：dry_run 报告 / 真实 run 写缓存 ──────────────────────────────


def _seed_hot(fake_redis, query: str, count: int, threshold: int, monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:cache_threshold": threshold,
    }))
    for _ in range(count):
        _run(dreaming.record_query_frequency(query))


def test_consolidate_dry_run_reports_without_writing(fake_redis, monkeypatch):
    _seed_hot(fake_redis, "popular query", 4, 2, monkeypatch)
    action = _run(dreaming._consolidate_hot_queries("alpha", dry_run=True))
    assert action["summary"] == "candidates_found"
    assert action["candidates"] == 1
    assert action["candidate_queries"] == [{"query": "popular query", "count": 4}]
    assert action["applied"] is False
    assert not any(k.startswith("dream:cache:hot:") for k in fake_redis.strings)


def test_consolidate_real_run_writes_hot_cache(fake_redis, monkeypatch):
    _seed_hot(fake_redis, "popular query", 4, 2, monkeypatch)
    action = _run(dreaming._consolidate_hot_queries("alpha", dry_run=False))
    assert action["summary"] == "promoted_to_hot_cache"
    assert action["applied"] is True
    assert action["written"] == 1
    key = f"dream:cache:hot:{dreaming.query_hash('popular query')}"
    payload = json.loads(fake_redis.strings[key])
    assert payload == {"query": "popular query", "count": 4, "date": _day()}
    assert fake_redis.ttls[key] == 24 * 3600


def test_consolidate_no_data_when_nothing_recorded(fake_redis):
    action = _run(dreaming._consolidate_hot_queries("alpha", dry_run=True))
    assert action["summary"] == "no_data"
    assert action["scanned"] == 0
    assert action["applied"] is False


def test_consolidate_no_candidates_reports_scanned(fake_redis, monkeypatch):
    _seed_hot(fake_redis, "rare query", 1, 5, monkeypatch)
    action = _run(dreaming._consolidate_hot_queries("alpha", dry_run=False))
    assert action["summary"] == "no_candidates"
    assert action["scanned"] == 1
    assert action["applied"] is False
    assert not any(k.startswith("dream:cache:hot:") for k in fake_redis.strings)


# ── run_dream_cycle 全链路（计数 → 候选 → 热缓存） ───────────────────────────


def test_cycle_full_chain_promotes_hot_queries(fake_redis, monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:cache_threshold": 2,
        "config:dreaming:graph_prune_enabled": True,
        "config:dreaming:prune_apply": False,
    }))
    monkeypatch.setattr(dreaming, "_list_enabled_containers", lambda scope: ["alpha"])
    for _ in range(3):
        _run(dreaming.record_query_frequency("Chain Query"))
    rep = _run(dreaming.run_dream_cycle(container="alpha", dry_run=False))
    assert rep["status"] == "ok"
    consolidate = next(
        a for a in rep["actions"] if a["tool"] == "consolidate_hot_queries"
    )
    assert consolidate["summary"] == "promoted_to_hot_cache"
    assert consolidate["applied"] is True
    key = f"dream:cache:hot:{dreaming.query_hash('chain query')}"
    assert key in fake_redis.strings
    # 破坏性动作仍 report-only（prune_apply=false 双保险不受影响）
    prune = [a for a in rep["actions"] if a["tool"].startswith("prune_")]
    assert prune and all(a["applied"] is False for a in prune)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
