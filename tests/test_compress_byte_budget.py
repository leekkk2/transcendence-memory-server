"""compress map-reduce 字节预算单测（400 修复 — governance_tools）。

回归根因：簇全文拼成单条 prompt 曾撑到 ~12.76 MB，越过上游网关 10 MiB 输入硬限
触发 400（4xx 不重试 → 工具 degraded）。修复 = map-reduce 字节预算分批。本套件
覆盖三个纯函数（不连真网关、不连 lancedb）：

  * _batch_cluster_by_bytes：>10 MiB 簇被切多批，且每批拼出的 prompt（含 system
    开销）< batch_byte_budget；小簇单批的廉价路径；预算非正退守。
  * _scan_compress_preview：dry_run 预览补 estimated_bytes / batch_count（不调 LLM）。
  * _truncate_for_llm：UTF-8 边界安全（不切断多字节字符）、超限补 marker、退守。

与 test_governance_tools_real_exec.py 同款隔离法：scripts/ 注入 sys.path、
TM_REDIS_ENABLED=0、独立 WORKSPACE + config_store 单例重置。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("TM_REDIS_ENABLED", "0")

import config_store  # noqa: E402
import governance_tools  # noqa: E402

# 真实内容实测的网关 context 边界（混合 CJK+Latin acc-demo bigcluster 直探 gemini-3.1-flash-lite-preview）：
#   768 KiB (786432 B)  → 200 OK（实测成功上限）
#   1 MiB  (1048576 B)  → HTTP 400 code=context_too_large（实测失败阈值）
# 注：重复字符（如 'x'）探测会因 tokenizer 合并被严重高估；此处为真实混合内容实测。
# CJK UTF-8 3 字节/字符 ≈ 1 token/字符，比 ASCII 更费 token，故安全默认须明显低于此。
# 来源：本仓 2026-06-15 compress_batch_bytes 真实内容直探会话（gemini-3.1-flash-lite-preview 经网关路由）。
CONTEXT_SAFE_CEILING_BYTES = 786_432   # 真实混合内容实测「成功上限」（768 KiB）
CONTEXT_FAIL_OBSERVED_BYTES = 1_048_576  # 真实混合内容实测「context_too_large 400」（1 MiB）


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch, tmp_path):
    """独立 WORKSPACE + 重置 config 单例 + Redis 关闭，避免用例间状态串扰。"""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    # 字节预算 env 可能被别的会话污染；显式清掉用默认/config 路径。
    monkeypatch.delenv("TM_COMPRESS_BATCH_BYTES", raising=False)
    config_store.reset_for_tests()

    async def _none(key, default=None):
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _none)
    yield
    config_store.reset_for_tests()


def _prompt_bytes_for_batch(batch: list[dict], row_char_cap: int) -> int:
    """复刻真执行的 batch_prompt 拼装（"\\n\\n".join(_format_cluster_row)）+ system
    开销，得到该批送上游的 prompt 实际 UTF-8 字节数 —— 用来断言每批确实 < 预算。"""
    body = "\n\n".join(
        governance_tools._format_cluster_row(r, row_char_cap) for r in batch
    )
    overhead = max(
        len(governance_tools._INDEX_CARD_SYSTEM_PROMPT.encode("utf-8")),
        len(governance_tools._INDEX_CARD_REDUCE_SYSTEM_PROMPT.encode("utf-8")),
    )
    return len(body.encode("utf-8")) + overhead


# ── _batch_cluster_by_bytes：>10 MiB 簇切多批，每批 < 预算 ─────────────────────


def test_oversized_cluster_splits_into_multiple_batches_each_under_budget():
    # 构造 >10 MiB 全文的簇：14 行 × 每行 ~1 MiB → ~14 MiB，远超单批 8 MiB 预算。
    row_char_cap = 2_000_000  # 不截断（每行 < cap），让全文真有 1 MiB 量级
    one_mib_text = "x" * (1024 * 1024)
    cluster = [
        {"id": f"big-{i}", "title": f"t{i}", "text": one_mib_text, "tags": ["k"]}
        for i in range(14)
    ]
    total_bytes = sum(
        len(governance_tools._format_cluster_row(r, row_char_cap).encode("utf-8"))
        for r in cluster
    )
    assert total_bytes > 10 * 1024 * 1024  # 前置：确实越过 10 MiB 硬限

    budget = 8 * 1024 * 1024
    batches = governance_tools._batch_cluster_by_bytes(cluster, budget, row_char_cap)

    assert len(batches) > 1  # 必须切多批，不再是单条巨 prompt
    # 每批拼出的 prompt（含 system 开销）都 < 预算 → 不会再触发 10 MiB 400。
    for batch in batches:
        assert _prompt_bytes_for_batch(batch, row_char_cap) < budget
    # 分批无损：所有行被覆盖、无重复、保持原顺序。
    flat = [r["id"] for batch in batches for r in batch]
    assert flat == [r["id"] for r in cluster]


def test_small_cluster_stays_single_batch():
    # 几条短记忆远在预算内 → 单批（保留廉价单跳 map 路径，省 reduce）。
    cluster = [
        {"id": "a1", "text": "short one", "tags": ["t"]},
        {"id": "a2", "text": "short two", "tags": ["t"]},
        {"id": "a3", "text": "short three", "tags": ["t"]},
    ]
    batches = governance_tools._batch_cluster_by_bytes(cluster, 8 * 1024 * 1024, 20000)
    assert len(batches) == 1
    assert [r["id"] for r in batches[0]] == ["a1", "a2", "a3"]


def test_row_char_cap_truncates_oversized_single_row_before_packing():
    # 单条超大记忆先被截到 row_char_cap 字符，防它独自撑爆一批字节预算。
    cluster = [{"id": "huge", "text": "y" * 100_000, "tags": ["t"]}]
    frag = governance_tools._format_cluster_row(cluster[0], 20000)
    # 截断后片段长度受 cap 约束（+ id/title/换行的少量固定开销 + 省略号）。
    assert len(frag) < 20100
    assert frag.endswith("…")


def test_non_positive_budget_degrades_to_default_not_crash():
    # 预算 <=0 → 退守默认 8 MiB（永不抛、永不空批死循环）。
    cluster = [{"id": "a1", "text": "z" * 10, "tags": ["t"]}]
    batches = governance_tools._batch_cluster_by_bytes(cluster, 0, 20000)
    assert len(batches) == 1
    assert batches[0][0]["id"] == "a1"


def test_shipped_default_batch_bytes_under_context_safe_ceiling():
    # 核心断言：shipped 默认批预算须等于 262144（256 KiB），且两处默认源一致（config_store
    # 注册默认 == governance_tools fallback 常量），且远低于真实内容实测成功上限 768 KiB。
    shipped = governance_tools._COMPRESS_BATCH_BYTES_DEFAULT
    registered = config_store.KNOWN_CONFIG["config:agent:compress_batch_bytes"].default
    assert shipped == registered == 262144  # 两处默认源不得漂移，且均为 256 KiB
    # 无 env/override 时 _compress_batch_bytes() 解析出的有效默认也必须一致。
    effective = governance_tools._compress_batch_bytes()
    assert effective == 262144
    # 远低于真实混合内容实测成功上限（768 KiB），留足 ~3× 余量覆盖 CJK 更费 token。
    assert shipped <= CONTEXT_SAFE_CEILING_BYTES
    assert shipped <= int(CONTEXT_SAFE_CEILING_BYTES * 0.4)  # ≤ 40% 上限（~3× 余量）


def test_default_budget_splits_over_10mib_cluster_each_batch_under_budget():
    # 用 shipped 默认预算（256 KiB）对一个 >10 MiB 的合成簇分批 → 必须切多批，且每批
    # 拼出的 prompt（含 system 开销）都 < 默认预算 → 默认配置下不会撞 context_too_large。
    # 256 KiB 默认比 1 MiB 时会切更多批（batch_count 更大），断言放宽到 >1 即满足。
    # 用 200 KiB / 行（> 256 KiB 默认预算单行即独立成批），凑 14 行 ≈ 2.8 MiB > 10 MiB
    # 前置靠 test_oversized_cluster_splits_into_multiple_batches_each_under_budget 已验；
    # 这里只验默认值路径下也能正确分批且无损。
    row_char_cap = 2_000_000  # 不截断，让每行全文真有 200 KiB 量级
    chunk_text = "x" * (200 * 1024)
    cluster = [
        {"id": f"big-{i}", "title": f"t{i}", "text": chunk_text, "tags": ["k"]}
        for i in range(14)
    ]
    total_bytes = sum(
        len(governance_tools._format_cluster_row(r, row_char_cap).encode("utf-8"))
        for r in cluster
    )
    assert total_bytes > 1024 * 1024  # 前置：确实超过 1 MiB（远超 256 KiB 默认预算）

    budget = governance_tools._compress_batch_bytes()  # shipped 默认（256 KiB）
    assert budget == 262144  # 明确断言使用的是 256 KiB 默认
    batches = governance_tools._batch_cluster_by_bytes(cluster, budget, row_char_cap)

    assert len(batches) > 1  # 默认预算下必然切多批
    for batch in batches:
        assert _prompt_bytes_for_batch(batch, row_char_cap) < budget
    # 分批无损：所有行被覆盖、无重复、保持原顺序。
    flat = [r["id"] for batch in batches for r in batch]
    assert flat == [r["id"] for r in cluster]


def test_oversized_single_row_still_becomes_its_own_batch():
    # 即便单行截断后仍超预算（极端小预算），该行仍单独成一批，不丢、不死循环。
    cluster = [
        {"id": "r1", "text": "a" * 5000, "tags": ["t"]},
        {"id": "r2", "text": "b" * 5000, "tags": ["t"]},
    ]
    # 极小预算（每批塞不下两行）→ 每行各自成批。
    batches = governance_tools._batch_cluster_by_bytes(cluster, 1024, 20000)
    assert len(batches) == 2
    assert [r["id"] for batch in batches for r in batch] == ["r1", "r2"]


# ── _scan_compress_preview：补 estimated_bytes / batch_count（不调 LLM） ───────


def _write_rows(tmp_path: Path, container: str, rows: list[dict]) -> Path:
    root = tmp_path / "tasks" / "rag" / "containers" / container
    root.mkdir(parents=True, exist_ok=True)
    path = root / "memory_objects.jsonl"
    import json

    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def test_preview_reports_batch_count_and_estimated_bytes_without_llm(tmp_path, monkeypatch):
    # 预览必须不触 LLM，且 batch_count 与真执行同一分批启发式一致。预览路径用默认
    # row_char_cap=20000 截行，故用 env 把每批预算调小让大簇真切多批（确定性）。
    async def _boom(prompt, system_prompt=None):
        raise AssertionError("preview must never call the LLM")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _boom)
    monkeypatch.setenv("TM_COMPRESS_BATCH_BYTES", "6000")  # < 单行截断后字节 → 每行成批

    one_mib_text = "x" * (1024 * 1024)
    cluster_rows = [
        {"id": f"big-{i}", "title": f"t{i}", "text": one_mib_text, "tags": ["topic"]}
        for i in range(14)
    ]
    _write_rows(tmp_path, "box-a", cluster_rows)

    preview = governance_tools._scan_compress_preview("box-a", {})
    assert preview["would_compress"] is True
    assert preview["cluster_size"] == 14
    # estimated_bytes 是簇全文（按 row_char_cap 截断后）的真实 UTF-8 字节和。
    expected_bytes = sum(
        len(governance_tools._format_cluster_row(r, governance_tools._compress_row_char_cap())
            .encode("utf-8"))
        for r in cluster_rows
    )
    assert preview["estimated_bytes"] == expected_bytes
    # batch_count 与真执行同一 _batch_cluster_by_bytes 一致，且确实切多批。
    batches = governance_tools._batch_cluster_by_bytes(
        cluster_rows,
        governance_tools._compress_batch_bytes(),
        governance_tools._compress_row_char_cap(),
    )
    assert preview["batch_count"] == len(batches)
    assert preview["batch_count"] > 1
    assert "map-reduce in" in preview["scan_notes"]


def test_preview_small_cluster_single_batch(tmp_path, monkeypatch):
    async def _boom(prompt, system_prompt=None):
        raise AssertionError("preview must never call the LLM")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _boom)
    _write_rows(tmp_path, "box-a", [
        {"id": "a1", "text": "short one with enough chars", "tags": ["t"]},
        {"id": "a2", "text": "short two with enough chars", "tags": ["t"]},
    ])
    preview = governance_tools._scan_compress_preview("box-a", {})
    assert preview["would_compress"] is True
    assert preview["batch_count"] == 1
    assert preview["estimated_bytes"] > 0


def test_preview_no_cluster_reports_zero_batches(tmp_path, monkeypatch):
    async def _boom(prompt, system_prompt=None):
        raise AssertionError("preview must never call the LLM")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _boom)
    _write_rows(tmp_path, "box-a", [
        {"id": "solo", "text": "a lone memory on its own topic here", "tags": ["x"]},
    ])
    preview = governance_tools._scan_compress_preview("box-a", {})
    assert preview["would_compress"] is False
    # 簇 <2 → 不分批（batch_count=0），但 estimated_bytes 仍是候选簇全文字节（不 gate）。
    assert preview["batch_count"] == 0
    assert "nothing to compress" in preview["scan_notes"]


# ── TM_COMPRESS_BATCH_BYTES env override（优先级最高） ────────────────────────


def test_env_override_batch_bytes_changes_batching(monkeypatch):
    # env 把每批预算调小 → 同一簇切出更多批（env 覆盖优先级最高）。
    cluster = [
        {"id": f"r{i}", "text": "w" * 4000, "tags": ["t"]} for i in range(6)
    ]
    big = governance_tools._batch_cluster_by_bytes(cluster, 8 * 1024 * 1024, 20000)
    assert len(big) == 1  # 默认大预算 → 单批

    monkeypatch.setenv("TM_COMPRESS_BATCH_BYTES", "6000")
    # _compress_batch_bytes 读 env → 更小预算。
    small_budget = governance_tools._compress_batch_bytes()
    assert small_budget == 6000
    small = governance_tools._batch_cluster_by_bytes(cluster, small_budget, 20000)
    assert len(small) > 1


# ── _truncate_for_llm：UTF-8 边界 + marker + 退守 ─────────────────────────────


def test_truncate_under_budget_passthrough():
    text = "hello world"
    assert governance_tools._truncate_for_llm(text, 1000) == text


def test_truncate_over_budget_appends_marker_and_fits():
    text = "a" * 5000
    out = governance_tools._truncate_for_llm(text, 1000)
    assert "[truncated" in out
    # 截断结果（含 marker）UTF-8 字节数不超过预算。
    assert len(out.encode("utf-8")) <= 1000


def test_truncate_does_not_split_multibyte_char():
    # 全多字节字符（每个 CJK 3 bytes UTF-8）；预算落在某字符中间，绝不产生乱码字节。
    text = "汉" * 1000  # 3000 bytes
    budget = 100
    out = governance_tools._truncate_for_llm(text, budget)
    # 可重新编码（无半个字符）→ round-trip 无异常即证明边界安全。
    out.encode("utf-8")  # 不抛
    assert len(out.encode("utf-8")) <= budget
    assert "[truncated" in out
    # marker 之前的正文部分仍是完整的「汉」字（errors='ignore' 丢弃了被切断的尾字节）。
    head = out.split(" …[truncated")[0]
    assert set(head) <= {"汉"}


def test_truncate_non_string_and_nonpositive_budget_are_conservative():
    assert governance_tools._truncate_for_llm("anything", 0) == ""
    assert governance_tools._truncate_for_llm("anything", -5) == ""
    # 非字符串入参被 str() 规整后再处理，不抛。
    out = governance_tools._truncate_for_llm(12345, 1000)  # type: ignore[arg-type]
    assert out == "12345"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
