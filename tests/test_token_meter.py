"""token_meter 单测：验证 P3 全局 Token 追踪 + 额度熔断的核心不变量。

核心保证（与 redis_client P0 / config_store P1 同款 fail-open 哲学）：
  * record_usage 是 fire-and-forget：Redis 禁用/不可达时绝不抛、绝不阻塞。
  * over_budget fail-open + 默认 OFF：
      - 无预算配置（默认）→ over=False（= 现网行为，逐字节一致，且不碰 Redis）。
      - Redis down → over=False（治理失效优于误杀 Agent）。
      - 预算配置 + 超限 → over=True + fallback_model（真 redis 闭环，@integration）。
  * TokenBatcher flush → SQLite token_usage_rollup UPSERT 累加（DB 往返）。
  * agent_id / task_type 解析与默认。

不依赖外网：禁用路径纯逻辑无连接；真 Redis 计数/熔断闭环用 @pytest.mark.integration。
每个 test 用 monkeypatch 覆盖 env，并在前后 reset 进程级单例。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config_store  # noqa: E402
import redis_client  # noqa: E402
import token_meter  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个 test 前后清进程级单例：redis pool/client、token batcher、config 缓存。"""
    redis_client.reset_for_tests()
    token_meter.reset_for_tests()
    config_store.reset_for_tests()
    yield
    asyncio.run(redis_client.close_pool())
    redis_client.reset_for_tests()
    token_meter.reset_for_tests()
    config_store.reset_for_tests()


# ── (a) Redis 禁用 → record_usage 不抛、over_budget fail-open 返 False ────────


def test_record_usage_no_raise_when_redis_disabled(monkeypatch):
    """Redis 禁用时 record_usage 整段降级 no-op，绝不抛。"""
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    redis_client.reset_for_tests()
    # 不应抛任何异常（无 batcher 也无 Redis → 纯 no-op）。
    asyncio.run(
        token_meter.record_usage("gpt-x", "rag_retrieval", "agentA", 100, 50)
    )


def test_over_budget_failopen_when_redis_disabled(monkeypatch):
    """预算已配置但 Redis 禁用 → fail-open 返 over=False（不误杀 Agent）。"""
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    redis_client.reset_for_tests()
    # 直接把预算塞进 config 进程缓存（绕开 DB/Redis 写），模拟 founder 配了预算。
    config_store._cache_put("config:token:hourly_budget", "1000")
    config_store._cache_put("config:token:daily_budget", "5000")

    decision = asyncio.run(token_meter.over_budget("agentA", "gpt-x"))
    # Redis 无计数可读 → _window_total 返 0 < budget → over=False（也即 fail-open）。
    assert decision["over"] is False
    assert decision["scope"] is None


# ── (b) 无预算配置 → over_budget 恒 False（现网行为，且不碰 Redis）───────────


def test_over_budget_off_by_default(monkeypatch):
    """默认无预算 → over=False，且短路在 over_budget 内部不触发任何 Redis 读。"""
    # 不放任何 config:token:*budget 进缓存 → get_cached 返回 default None。
    # 哪怕 Redis "可用"也不应被触达：用一个会抛的桩证明短路。
    called = {"hgetall": False}

    async def _boom(*_a, **_k):
        called["hgetall"] = True
        raise AssertionError("over_budget must short-circuit before reading Redis")

    monkeypatch.setattr(redis_client, "hgetall", _boom)

    decision = asyncio.run(token_meter.over_budget("agentA", "gpt-x"))
    assert decision == {"over": False, "scope": None, "fallback_model": None}
    assert called["hgetall"] is False


def test_zero_budget_means_unlimited(monkeypatch):
    """预算配成 0 → 等同无限额（enforcement off），over 恒 False。"""
    config_store._cache_put("config:token:hourly_budget", "0")
    config_store._cache_put("config:token:daily_budget", "0")
    decision = asyncio.run(token_meter.over_budget("agentA", "gpt-x"))
    assert decision["over"] is False


# ── (e) agent_id / task_type 解析与默认 ──────────────────────────────────────


def test_resolve_agent_id_priority():
    # 显式 header 优先
    assert token_meter.resolve_agent_id("agent-coding", "deadbeef") == "agent-coding"
    # 无 header → 用 api_key_hash
    assert token_meter.resolve_agent_id(None, "deadbeef") == "deadbeef"
    assert token_meter.resolve_agent_id("  ", "deadbeef") == "deadbeef"
    # 都没有 → 默认 unknown
    assert token_meter.resolve_agent_id(None, None) == token_meter.DEFAULT_AGENT_ID


def test_contextvar_defaults_and_set():
    # 默认值
    assert token_meter.get_task_type() == token_meter.DEFAULT_TASK_TYPE
    assert token_meter.get_agent_id() == token_meter.DEFAULT_AGENT_ID
    # set 后读回
    token_meter.set_task_type("agent_coding")
    token_meter.set_agent_id("agentZ")
    assert token_meter.get_task_type() == "agent_coding"
    assert token_meter.get_agent_id() == "agentZ"
    # 空值回落默认
    token_meter.set_task_type(None)
    token_meter.set_agent_id("")
    assert token_meter.get_task_type() == token_meter.DEFAULT_TASK_TYPE
    assert token_meter.get_agent_id() == token_meter.DEFAULT_AGENT_ID


# ── (d) TokenBatcher flush → SQLite rollup 累加（DB 往返，无外网）─────────────


def test_batcher_flush_upserts_and_accumulates(tmp_path):
    """两次 add 同一 (day,model,task,agent) → flush 后 SQLite 累加到一行。"""
    db = tmp_path / "queue.db"

    async def _run() -> None:
        batcher = token_meter.TokenBatcher(db, flush_interval=60.0)
        token_meter.ensure_schema(db)
        batcher.add(
            {
                "day": "2026-06-09", "model": "gpt-x", "task_type": "rag_retrieval",
                "agent_id": "agentA", "prompt_tokens": 100,
                "completion_tokens": 50, "total_tokens": 150,
            }
        )
        batcher.add(
            {
                "day": "2026-06-09", "model": "gpt-x", "task_type": "rag_retrieval",
                "agent_id": "agentA", "prompt_tokens": 10,
                "completion_tokens": 5, "total_tokens": 15,
            }
        )
        await batcher.flush()

    asyncio.run(_run())

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM token_usage_rollup WHERE agent_id='agentA'"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["prompt_tokens"] == 110
    assert r["completion_tokens"] == 55
    assert r["total_tokens"] == 165


def test_batcher_second_flush_accumulates_existing_row(tmp_path):
    """第二次 flush 应在已有行上继续累加（验证 ON CONFLICT UPDATE 累加，而非覆盖）。"""
    db = tmp_path / "queue.db"

    async def _run() -> None:
        batcher = token_meter.TokenBatcher(db, flush_interval=60.0)
        token_meter.ensure_schema(db)
        batcher.add({
            "day": "2026-06-09", "model": "m", "task_type": "t",
            "agent_id": "a", "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
        })
        await batcher.flush()
        batcher.add({
            "day": "2026-06-09", "model": "m", "task_type": "t",
            "agent_id": "a", "prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9,
        })
        await batcher.flush()

    asyncio.run(_run())
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT total_tokens FROM token_usage_rollup WHERE agent_id='a'"
        ).fetchone()
    assert row[0] == 12


def test_usage_summary_groups_dimensions(tmp_path):
    """usage_summary 按 model/task/agent 分组聚合 + totals。"""
    db = tmp_path / "queue.db"
    token_meter.ensure_schema(db)
    with sqlite3.connect(str(db)) as conn:
        conn.executemany(
            "INSERT INTO token_usage_rollup "
            "(day, model, task_type, agent_id, prompt_tokens, completion_tokens, total_tokens) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                ("2026-06-09", "gpt-x", "rag_retrieval", "agentA", 100, 50, 150),
                ("2026-06-09", "gpt-y", "agent_coding", "agentB", 200, 100, 300),
            ],
        )
        conn.commit()

    data = token_meter.usage_summary(db, window="all")
    assert data["totals"]["total_tokens"] == 450
    assert data["totals"]["prompt_tokens"] == 300
    models = {r["key"]: r["total_tokens"] for r in data["by_model"]}
    assert models == {"gpt-x": 150, "gpt-y": 300}
    agents = {r["key"] for r in data["by_agent"]}
    assert agents == {"agentA", "agentB"}


# ── record_usage 零 token 跳过 ───────────────────────────────────────────────


def test_record_usage_skips_zero_tokens(monkeypatch):
    """usage 为空（无 token）→ 不写任何计数（不触发 Redis hincr）。"""
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    redis_client.reset_for_tests()
    called = {"hincr": 0}

    async def _spy(*_a, **_k):
        called["hincr"] += 1
        return None

    monkeypatch.setattr(redis_client, "hincr", _spy)
    asyncio.run(token_meter.record_usage("m", "t", "a", 0, 0))
    assert called["hincr"] == 0


# ── 真 Redis 集成：计数累加 + 超限熔断闭环（pytest -m integration）────────────


@pytest.mark.integration
def test_real_redis_count_and_breaker(monkeypatch):
    """需 REDIS_URL 指向真 Redis。验证：
    1) record_usage 把 token 累加进 daily/hourly 计数（含 per-agent 聚合 hash）。
    2) 配 hourly_budget 且累计超限 → over_budget 返 over=True + scope + 置熔断标记。

    单一事件循环内完成所有 await（redis.asyncio client 绑定首个使用它的 loop）。
    """
    pytest.importorskip("redis.asyncio")
    import os

    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set — provide a live Redis to run this")
    monkeypatch.setenv("TM_REDIS_ENABLED", "1")
    redis_client.reset_for_tests()
    # 配一个很小的 hourly 预算，确保两次 record 即可超限。
    config_store._cache_put("config:token:hourly_budget", "200")
    config_store._cache_put("config:token:fallback_model", "ollama/qwen3")

    agent = "tm-test-agentZ"

    async def _flow() -> None:
        assert await redis_client.is_available() is True
        # 累加 300 total（>200 预算）。
        await token_meter.record_usage("gpt-x", "rag_retrieval", agent, 200, 100)
        decision = await token_meter.over_budget(agent, "gpt-x")
        assert decision["over"] is True
        assert decision["scope"] == "hourly"
        assert decision["fallback_model"] == "ollama/qwen3"
        # 熔断标记已写入
        marker = await redis_client.get_str(token_meter._breaker_key(agent))
        assert marker == "hourly"
        # 清理本测试写入的 key（同一 loop 内）。
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        client = await redis_client.get_client()
        if client is not None:
            await client.delete(
                token_meter._daily_key("gpt-x", "rag_retrieval", agent, now),
                token_meter._hourly_key("gpt-x", "rag_retrieval", agent, now),
                token_meter._agent_total_key(
                    token_meter._DAILY_PREFIX, now.strftime("%Y%m%d"), agent),
                token_meter._agent_total_key(
                    token_meter._HOURLY_PREFIX, now.strftime("%Y%m%d%H"), agent),
                token_meter._breaker_key(agent),
            )
        await redis_client.close_pool()

    asyncio.run(_flow())
