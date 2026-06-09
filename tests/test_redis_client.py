"""redis_client 单测：核心验证「优雅降级」不变量——Redis 不可达/禁用时
绝不抛异常，安全 helper 退回 default。

不依赖外网：
  * 禁用路径（TM_REDIS_ENABLED=0）纯逻辑，无连接，无条件运行。
  * dead-port 路径需要 `redis` 包才能构造异步客户端 → importorskip，
    本机/CI 若未装 redis 则跳过（仍在装了 redis 的环境里验证降级行为）。
  * 真 Redis 集成测试用 @pytest.mark.integration 标注，冒烟阶段单独跑。

每个 test 用 monkeypatch 覆盖 env，并在前后 reset_for_tests() 清进程级单例，
避免测试间串状态。async 函数统一 asyncio.run(...)，与仓内既有约定一致。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import redis_client  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_singletons():
    """每个 test 前后清掉进程级 pool/client 单例 + 禁用日志闩。"""
    redis_client.reset_for_tests()
    yield
    asyncio.run(redis_client.close_pool())
    redis_client.reset_for_tests()


# ── (b) TM_REDIS_ENABLED=0 → 全程禁用，无连接，helper 退回 default ──────────


def test_disabled_via_env_short_circuits(monkeypatch):
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client.reset_for_tests()

    assert redis_client.is_enabled() is False
    # init_pool 返回 False，且绝不建连/不抛
    assert asyncio.run(redis_client.init_pool()) is False
    # get_client 返回 None
    assert asyncio.run(redis_client.get_client()) is None
    # is_available 返回 False（不抛）
    assert asyncio.run(redis_client.is_available()) is False
    # 安全 helper 退回 default
    assert asyncio.run(redis_client.cfg_get("config:rag:similarity_threshold", "0.7")) == "0.7"
    assert asyncio.run(redis_client.cfg_get("missing-key")) is None
    # 写入降级为 no-op，返回 False（不抛）
    assert asyncio.run(redis_client.cfg_set("k", "v")) is False


def test_no_url_means_disabled(monkeypatch):
    """没有 REDIS_URL 也没有 REDIS_HOST → 视为未配置（禁用）。"""
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    monkeypatch.delenv("TM_REDIS_ENABLED", raising=False)
    redis_client.reset_for_tests()

    assert redis_client.is_enabled() is False
    assert asyncio.run(redis_client.is_available()) is False
    assert asyncio.run(redis_client.cfg_get("any", "fallback")) == "fallback"


def test_url_resolution_priority(monkeypatch):
    """REDIS_URL 优先；缺省时由 HOST/PORT/PASSWORD 合成。纯逻辑，不建连。"""
    monkeypatch.setenv("REDIS_URL", "redis://explicit:6380/2")
    assert redis_client._resolve_url() == "redis://explicit:6380/2"

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_HOST", "myhost")
    monkeypatch.setenv("REDIS_PORT", "6399")
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)
    assert redis_client._resolve_url() == "redis://myhost:6399/0"

    monkeypatch.setenv("REDIS_PASSWORD", "s3cret")
    assert redis_client._resolve_url() == "redis://:s3cret@myhost:6399/0"


# ── (a) 指向 dead 端口 → is_available()==False 且不抛，cfg_get 退回 default ──


def test_dead_port_degrades_without_raising(monkeypatch):
    # 需要真实 redis 包来构造异步客户端；未安装则跳过（装了的环境仍验证降级）。
    pytest.importorskip("redis.asyncio")
    # 选一个几乎不可能有服务监听的端口（reserved/discard 之上的高位）。
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    monkeypatch.setenv("TM_REDIS_ENABLED", "1")
    # 缩短超时让本测试快速失败（不长时间挂起）。
    monkeypatch.setenv("TM_REDIS_SOCKET_TIMEOUT", "0.3")
    monkeypatch.setenv("TM_REDIS_CONNECT_TIMEOUT", "0.3")
    redis_client.reset_for_tests()

    # 配置层启用，但实际连不上
    assert redis_client.is_enabled() is True
    # init_pool 成功建 pool（不建连接 → 不报错）
    assert asyncio.run(redis_client.init_pool()) is True
    # get_client 返回非 None 客户端对象（连接 lazy）
    assert asyncio.run(redis_client.get_client()) is not None
    # ping 失败 → is_available 返回 False（吞异常，不抛）
    assert asyncio.run(redis_client.is_available()) is False
    # 安全读 helper 退回 default（不抛）
    assert asyncio.run(redis_client.cfg_get("config:rag:degradation_timeout_ms", "800")) == "800"
    # 安全写 helper 返回 False（不抛）
    assert asyncio.run(redis_client.cfg_set("config:probe", "1")) is False


# ── 真 Redis 集成（冒烟阶段单独跑：pytest -m integration）────────────────────


@pytest.mark.integration
def test_real_redis_roundtrip(monkeypatch):
    """需 REDIS_URL 指向真 Redis。验证 ping 通 + set/get 往返。

    整个往返必须在**单一事件循环**内完成：redis.asyncio 的 client/连接绑定到首次
    使用它的 loop，跨 `asyncio.run`（各自独立 loop）复用进程级 `_CLIENT` 单例会被
    拒绝 → cfg_set 被吞返 False、测试假性失败。故把 is_available → cfg_set →
    cfg_get → 清理串在一个 async 函数体内顺序 await，外层只调一次 asyncio.run。
    """
    pytest.importorskip("redis.asyncio")
    import os

    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set — provide a live Redis to run this")
    monkeypatch.setenv("TM_REDIS_ENABLED", "1")
    redis_client.reset_for_tests()

    async def _roundtrip() -> None:
        assert await redis_client.is_available() is True
        key = "tm:test:redis_client:roundtrip"
        assert await redis_client.cfg_set(key, "pong", ttl=30) is True
        assert await redis_client.cfg_get(key) == "pong"
        # 在同一 loop 内清掉 key + 关连接，避免污染并让 autouse 的 close_pool 幂等空跑。
        client = await redis_client.get_client()
        if client is not None:
            await client.delete(key)
        await redis_client.close_pool()

    asyncio.run(_roundtrip())
