"""config_store 单测：核心验证「优雅降级」+「行为保持」不变量。

P1 配置中心三层（SQLite config_kv → Redis cache → 进程内存缓存）+ Pub/Sub 热
重载。本套用例聚焦默认跑（不依赖外网）：

  * (a) 无 Redis/DB 时 get_cached 全退 default、set 不抛纯降级。
  * (b) DB 往返：set → DB 持久 → 重 load_all → get_cached 反映。
  * (c) 类型 coerce：int / float / bool / None。
  * (d) HR-9 守卫：set config:model:base_url 非许可网关 host 被拒。
  * (e) 行为保持：未 set similarity_threshold 时 get_cached 返回传入的 None default。

真 Redis 的 pub/sub 刷新用 @pytest.mark.integration（冒烟阶段单独跑）。

每个 test 用 monkeypatch 覆盖 WORKSPACE（隔离 queue.db）并 reset_for_tests()
清进程级单例，避免测试间串状态。async 函数统一 asyncio.run(...)。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import config_store  # noqa: E402
import redis_client  # noqa: E402


# 占位测试网关主机（脱敏，绝不用 founder 真实私域）；通过 TM_ALLOWED_MODEL_HOST
# 注入，与生产部署侧注入真实值的机制一致。
_TEST_GW_HOST = "gw.example"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个 test 用独立 WORKSPACE（→ 独立 queue.db）+ 默认禁用 Redis + 占位网关主机。"""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    # 默认禁用 Redis：纯 DB + 内存缓存路径，不建任何连接、不依赖外网。
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("REDIS_HOST", raising=False)
    # HR-9 host-pin：注入占位主机后重新解析模块级常量（import 时已读取一次）。
    monkeypatch.setenv("TM_ALLOWED_MODEL_HOST", _TEST_GW_HOST)
    monkeypatch.setattr(config_store, "_ALLOWED_MODEL_BASE_HOST", _TEST_GW_HOST)
    config_store.reset_for_tests()
    redis_client.reset_for_tests()
    yield
    config_store.reset_for_tests()
    redis_client.reset_for_tests()


# ── (a) 无 Redis/DB → get_cached 全退 default、set 不抛 ──────────────────────


def test_cache_empty_returns_default():
    """冷启动、未 load、未 set → 任何 known key 都退 default（行为保持的根）。"""
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None
    assert config_store.get_cached("config:rag:citation_enabled", True) is True
    assert config_store.get_cached("config:rag:degradation_timeout_ms", 800) == 800
    # 未知 key 也退 default，不抛。
    assert config_store.get_cached("config:does:not:exist", "fallback") == "fallback"


def test_set_unknown_key_rejected_no_raise():
    """set 未注册 key → False（拒绝），不抛、不落库。"""
    assert asyncio.run(config_store.set("config:bogus:key", "v")) is False


def test_set_does_not_raise_when_redis_disabled():
    """Redis 禁用时 set 仍走 DB + 本地 cache，返回 True（publish/cfg_set 静默 no-op）。"""
    ok = asyncio.run(config_store.set("config:rag:citation_enabled", False))
    assert ok is True
    # 本进程 cache 立即生效。
    assert config_store.get_cached("config:rag:citation_enabled", True) is False


def test_set_clear_writer_node_converges_to_caller_default():
    """回归（reviewer P2 #1 major）：写节点 clear override 后收敛到 caller 静态默认。

    set(citation_enabled, None) 清除覆盖后，写节点 get_cached 必须返回 caller 传入的
    静态默认（True），而非 coerced-NULL（_coerce_bool(None)=False）。修复前 set 对
    stored=None 走 _cache_put(key, None) → get_cached 找到 present NULL → coerce 成
    False（reset-to-default 回归）。修复后写节点 _cache_evict → 退 caller 默认，
    与对端 refresh 见 present-but-NULL 行 evict 的收敛行为完全一致。

    同时验证 DB 仍保留该键的 present-but-NULL 行（供对端 refresh evict 传播，不删行）。
    """
    key = "config:rag:citation_enabled"
    # 先设非默认值 False，再清除为 None。
    assert asyncio.run(config_store.set(key, False)) is True
    assert config_store.get_cached(key, True) is False
    assert asyncio.run(config_store.set(key, None)) is True
    # 写节点：清除后退 caller 静态默认 True（非 coerced-NULL False）。
    assert config_store.get_cached(key, True) is True
    # DB 仍存在该键的 present-but-NULL 行（found=True, value=None）—— 不删行。
    store = config_store._get_store()
    found, raw = store.get_row(key)
    assert found is True
    assert raw is None


def test_describe_key_is_override_false_after_clear():
    """回归（reviewer P2 #2 minor）：clear override 后 describe_key is_override=False。

    set(非默认)→is_override True；set(None) clear 后 describe 该键 is_override 必须
    False（UI 'modified' 徽章不再残留）。修复前 describe_key 用 get_row found-ness
    推 is_override，present-but-NULL 行仍 found=True → is_override 错为 True。
    """
    key = "config:rag:similarity_threshold"
    # set 非默认值 → is_override True。
    assert asyncio.run(config_store.set(key, 0.42)) is True
    d = config_store.describe_key(key)
    assert d["is_override"] is True
    assert d["value"] == pytest.approx(0.42)
    # clear（set None）→ present-but-NULL 行 → is_override False、value 退 default。
    assert asyncio.run(config_store.set(key, None)) is True
    d2 = config_store.describe_key(key)
    assert d2["is_override"] is False
    assert d2["value"] is None  # 退 registered default


def test_describe_all_user_friendly_metadata_complete():
    """Dashboard 元数据补全：全部 known key 都下发中文 group / label / description。

    防回归「dashboard 暴露裸 key」：describe_all（= GET /admin/config 数据源）的
    每一项都必须带非空 label 与 description（一句话用户向说明），group 不退
    module 裸名。老键（rag/model/token）与 P6 新键一视同仁。
    """
    items = config_store.describe_all()
    assert len(items) == len(config_store.KNOWN_CONFIG)
    for d in items:
        assert d["label"], f"{d['key']} missing label"
        assert d["description"], f"{d['key']} missing description"
        assert d["group"], f"{d['key']} missing group"
        # label/description 必须是人话，不是裸 key 尾巴照搬技术名以外的空值。
        assert isinstance(d["description"], str)
    # 抽样：老键（P6 前无 label/group）现在也有用户友好元数据。
    d = config_store.describe_key("config:rag:similarity_threshold")
    assert d["label"] == "检索相关性门槛"
    assert d["group"] == "检索与引用"
    assert "门槛" in d["description"]
    d = config_store.describe_key("config:token:daily_budget")
    assert d["group"] == "用量与预算"
    assert "上限" in d["description"]
    # 抽样：P6 新键 description 补齐（label/group 原有保持不动）。
    d = config_store.describe_key("config:dreaming:prune_apply")
    assert d["label"] == "梦境破坏性删除生效"
    assert d["description"]
    # 敏感键同样下发 description（但 value 仍恒 None，铁律不变）。
    d = config_store.describe_key("config:model:api_keys:llm")
    assert d["description"]
    assert d["value"] is None


def test_describe_key_sensitive_configured_false_after_clear():
    """回归（reviewer P2 #3 配套）：sensitive 键 PUT value:'' 清除后 configured=False。

    set(secret, 非空) → configured True；set(secret, '') 清除 → configured False
    且 is_override False。空串对敏感键存 NULL 行（非加密空串），与非敏感 clear 一致，
    既报 not-configured 又能经 refresh evict 传播到对端。永不回显明文/密文。
    """
    key = "config:model:api_keys:llm"
    assert asyncio.run(config_store.set(key, "sk-real-secret")) is True
    d = config_store.describe_key(key)
    assert d["configured"] is True
    assert d["is_override"] is True
    assert d["value"] is None  # 敏感键永不回显
    # PUT value:'' 清除 → configured False。
    assert asyncio.run(config_store.set(key, "")) is True
    d2 = config_store.describe_key(key)
    assert d2["configured"] is False
    assert d2["is_override"] is False
    assert d2["value"] is None
    # DB 行存在但 NULL（非加密空串）—— 供对端 refresh evict。
    store = config_store._get_store()
    found, raw = store.get_row(key)
    assert found is True
    assert raw is None


# ── (b) DB 往返：set → DB 持久 → 重 load_all → get_cached 反映 ───────────────


def test_db_roundtrip_survives_cache_reset():
    """set 落 DB；reset_for_tests 清内存 cache 后 get_cached 退 default；load_all 后恢复。"""
    assert asyncio.run(config_store.set("config:rag:similarity_threshold", 0.42)) is True
    assert config_store.get_cached("config:rag:similarity_threshold", None) == 0.42

    # 清内存缓存（模拟重启 / 新节点）—— DB 单例也清，强制重开。
    config_store.reset_for_tests()
    # cache 空 → 退 default。
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None

    # 从 DB 重新装载 → 反映持久值。
    asyncio.run(config_store.load_all())
    assert config_store.get_cached("config:rag:similarity_threshold", None) == 0.42


# ── (c) 类型 coerce：int / float / bool / None ──────────────────────────────


def test_coerce_int():
    asyncio.run(config_store.set("config:rag:degradation_timeout_ms", "1500"))
    assert config_store.get_cached("config:rag:degradation_timeout_ms", 0) == 1500
    assert isinstance(config_store.get_cached("config:rag:degradation_timeout_ms", 0), int)


def test_coerce_float():
    asyncio.run(config_store.set("config:rag:similarity_threshold", "0.73"))
    val = config_store.get_cached("config:rag:similarity_threshold", None)
    assert val == pytest.approx(0.73)
    assert isinstance(val, float)


def test_coerce_bool_truthy_and_falsy():
    asyncio.run(config_store.set("config:rag:citation_enabled", "0"))
    assert config_store.get_cached("config:rag:citation_enabled", True) is False
    asyncio.run(config_store.set("config:rag:citation_enabled", "on"))
    assert config_store.get_cached("config:rag:citation_enabled", False) is True


def test_coerce_none_sentinel_for_float_key():
    """float|None key：'none' / 'null' 等 OFF 哨兵串经 coerce 回 None。

    验证 coercer 的 None 哨兵语义本身（存非空哨兵串 → 读回 None），与「clear
    override」路径解耦。注意 set(key, None) 是「清除覆盖」而非「存 None 值」——
    清除后写节点 evict、get_cached 退 caller 静态默认（见
    test_set_clear_writer_node_converges_to_caller_default，reviewer P2 #1）。
    本用例改测存「字面 'none' 串」这一显式 OFF 覆盖：它是一个真实覆盖行，coerce
    回 None。
    """
    # 存字面 'none' 串 = 一个显式 OFF 覆盖（present 行、值非空），coerce 回 None。
    asyncio.run(config_store.set("config:rag:similarity_threshold", "none"))
    assert config_store.get_cached("config:rag:similarity_threshold", 0.5) is None


# ── (d) HR-9 守卫：base_url 非许可主机被拒（许可主机由 env 注入，测试用占位）─────


def test_hr9_rejects_foreign_base_url():
    """非许可网关主机的 base_url 覆盖被拒（不落库、不广播）。"""
    assert asyncio.run(
        config_store.set("config:model:base_url:llm", "https://api.openai.com/v1")
    ) is False
    # 也拒空值。
    assert asyncio.run(config_store.set("config:model:base_url:llm", "")) is False


def test_hr9_accepts_sanctioned_base_url():
    """许可网关主机（占位 gw.example，部署侧注入真实值）的 base_url 覆盖被接受。"""
    assert asyncio.run(
        config_store.set(
            "config:model:base_url:llm", f"https://{_TEST_GW_HOST}/v1"
        )
    ) is True


def test_sensitive_api_key_not_echoed_in_cache():
    """api_keys:* 敏感键 write-only：set 成功但不进 sync get_cached（不回显明文）。"""
    assert asyncio.run(
        config_store.set("config:model:api_keys:llm", "sk-secret-value")
    ) is True
    # 敏感键不进 request-path cache → get_cached 退 default（绝不回显明文）。
    got = config_store.get_cached("config:model:api_keys:llm", "<hidden>")
    assert got == "<hidden>"
    assert "sk-secret-value" not in str(got)


def test_sensitive_api_key_not_cached_after_load_all():
    """回归（reviewer #2）：敏感键持久化后，重启 load_all 不得把存储值缓进 cache。

    set 落 DB（加密或明文）→ reset_for_tests 清进程态 → load_all() 从 DB 重载。
    敏感键必须被 load_all 跳过（与 set/refresh 一致），get_cached 仍退 default，
    绝不回显存储的 secret/密文。修复前 load_all 无条件 _cache_put 会回显存储值。
    """
    assert asyncio.run(
        config_store.set("config:model:api_keys:llm", "sk-leaky-secret")
    ) is True
    config_store.reset_for_tests()  # 清内存 cache + DB 单例，模拟重启
    asyncio.run(config_store.load_all())  # 从 DB 重载
    got = config_store.get_cached("config:model:api_keys:llm", "<hidden>")
    assert got == "<hidden>"  # 退 default，未回显
    assert "sk-leaky-secret" not in str(got)
    assert "enc:" not in str(got)  # 也不回显密文


def test_refresh_propagates_override_clear_to_default():
    """回归（reviewer #3）：set(0.7)→set(None) 后，干净 cache refresh 收敛到 default。

    模拟两节点：节点 A 写 0.7 再清除（set None，DB 行存在但 value=NULL）。节点 B
    （reset 后干净 cache）refresh([key]) 必须把 cache 收敛到 default（非陈旧 0.7）。
    修复前 refresh 把「行存在但 NULL」与「DB miss」混为一谈 → 退 Redis 兜底，找不到
    则保留陈旧 cache，导致对端清除 override 后仍服务旧值直到重启。
    """
    key = "config:rag:similarity_threshold"
    # 节点 A：先设 0.7，再清除为 None（DB 行 value=NULL）。
    assert asyncio.run(config_store.set(key, 0.7)) is True
    assert asyncio.run(config_store.set(key, None)) is True
    # 节点 B：干净 cache，先人为塞入陈旧 0.7 模拟之前缓存过覆盖值。
    config_store._cache_put(key, "0.7")
    assert config_store.get_cached(key, None) == pytest.approx(0.7)
    # 收到 publish → refresh：DB 行存在但 NULL → evict → 收敛到 default(None)。
    asyncio.run(config_store.refresh([key]))
    assert config_store.get_cached(key, None) is None


def test_refresh_db_read_error_preserves_stale_cache():
    """回归（reviewer #3 注意项）：DB 完全不可达时 refresh 不抛、保留旧值不收敛。

    区分「行 NULL（清除）→ 收敛 default」与「DB 读故障 → 保留旧 cache」：后者绝不
    误当作清除把对端打回 default（否则瞬时读故障会丢失有效覆盖）。
    """
    key = "config:rag:similarity_threshold"
    config_store._cache_put(key, "0.55")  # 既有有效覆盖

    class _Boom:
        def get_row(self, k):
            raise RuntimeError("db unreachable")

    config_store._STORE = _Boom()  # 注入抛错的 store
    asyncio.run(config_store.refresh([key]))  # 不抛
    # Redis 禁用、DB 抛错 → 保留旧 cache（不收敛到 default）。
    assert config_store.get_cached(key, None) == pytest.approx(0.55)


# ── (e) 行为保持：未 set similarity_threshold → 返回传入的 None default ──────


def test_behavior_preserved_default_passthrough():
    """配置缺省时 get_cached 原样返回 caller 传入的 default（含 None / True）。

    这是 /search /query 行为逐字节不变的根：reader 把 profiles.yaml 静态值作
    default 传入，无覆盖时原样返回 → similarity_threshold 仍 None、citation 仍 True。
    """
    # similarity_threshold：profiles 默认 None → passthrough None。
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None
    # citation_enabled：profiles 默认 True → passthrough True。
    assert config_store.get_cached("config:rag:citation_enabled", True) is True
    # 任意非默认 default 也原样透传，证明 get_cached 不擅自改默认。
    assert config_store.get_cached("config:rag:similarity_threshold", 0.99) == 0.99


# ── 真 Redis 集成（pytest -m integration）──────────────────────────────────


@pytest.mark.integration
def test_pubsub_refresh_roundtrip(monkeypatch, tmp_path):
    """需 REDIS_URL 指向真 Redis：set→publish→subscriber refresh 闭环。

    单一事件循环内完成（redis.asyncio client 绑 loop，跨 asyncio.run 会被拒）。
    模拟两节点：用同一 DB，先起 subscriber，再 set 一个 key，验证 publish 触发
    refresh 后 cache 反映新值。
    """
    pytest.importorskip("redis.asyncio")
    import os

    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set — provide a live Redis to run this")
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "1")
    config_store.reset_for_tests()
    redis_client.reset_for_tests()

    async def _flow() -> None:
        assert await redis_client.is_available() is True
        await config_store.load_all()
        task = await config_store.start_config_subscriber()
        assert task is not None
        # 写一个 key → 触发 publish('config_updated') → subscriber refresh。
        assert await config_store.set("config:rag:similarity_threshold", 0.55) is True
        # 给订阅循环一点时间收到并刷新。
        await asyncio.sleep(0.5)
        assert config_store.get_cached("config:rag:similarity_threshold", None) == 0.55
        await config_store.stop_config_subscriber()
        await redis_client.close_pool()

    asyncio.run(_flow())
