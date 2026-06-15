"""治理工具真执行单测（dry_run=false 契约升级 — governance_tools）。

覆盖三个 LLM / 破坏性工具的真执行 handler：

* snapshot_and_quarantine：补充 gap_fixes 的真执行用例 —— governance sidecar
  文件落点 / 快照可逆性细节；
* compress_knowledge_cluster：LLM 索引卡附加式 append（源零删除）、簇 <2 短路、
  cluster_tag 指定簇、LLM 失败降级 error；
* tune_model_parameters：合法 JSON 全应用 / 越界全拒 / 部分应用 / 非 JSON
  error 零改动 / dry_run=True 三工具均不动数据。

LLM 一律 monkeypatch governance_tools._llm_oneshot —— 单测不连真网关。
与 test_governance_tools_p6.py 同款隔离法：scripts/ 注入 sys.path、
TM_REDIS_ENABLED=0、每例独立 WORKSPACE + config_store 单例重置（tune 用例
依赖真 config_store 写读回路，故不 mock get_cached，开关走默认全开）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

os.environ.setdefault("TM_REDIS_ENABLED", "0")

import config_store  # noqa: E402
import governance_tools  # noqa: E402
from task_rag_server_models import ToolInvokeResponse  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch, tmp_path):
    """独立 WORKSPACE + 重置 config 单例 + Redis 关闭，避免用例间状态串扰。"""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()

    async def _none(key, default=None):
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _none)
    yield
    config_store.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


def _write_rows(tmp_path: Path, container: str, rows: list[dict]) -> Path:
    root = tmp_path / "tasks" / "rag" / "containers" / container
    root.mkdir(parents=True, exist_ok=True)
    path = root / "memory_objects.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l]


def _llm_returning(text: str):
    async def _fake(prompt, system_prompt=None):
        return text

    return _fake


async def _llm_must_not_be_called(prompt, system_prompt=None):
    raise AssertionError("LLM must not be called on this path")


def _cluster_rows(now: int) -> list[dict]:
    return [
        {"id": "a1", "title": "t1", "text": "python rag note one with enough chars",
         "tags": ["python", "rag"], "updatedAt": now},
        {"id": "a2", "title": "t2", "text": "python rag note two with enough chars",
         "tags": ["python", "rag"], "updatedAt": now},
        {"id": "b1", "title": "solo", "text": "a lone memory on another topic here",
         "tags": ["other"], "updatedAt": now},
    ]


# ── compress_knowledge_cluster：真执行（附加式索引卡） ────────────────────────


def test_compress_not_dry_run_appends_index_card(tmp_path, monkeypatch):
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    captured: dict = {}

    async def _fake(prompt, system_prompt=None):
        captured["prompt"] = prompt
        captured["system_prompt"] = system_prompt
        return "假索引卡：同义词头部 / 核心结论 / 来源 id"

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _fake)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False
    ))
    assert res["status"] == "applied"
    assert res["applied"] is True
    result = res["result"]
    assert result["source_ids"] == ["a1", "a2"]
    assert result["cluster_tag"] == "python,rag"
    assert result["cluster_size"] == 2
    assert result["reindex_required"] is True
    # 新卡 append 在尾部；源记忆零删除（前 3 行原样）
    rows = _read_rows(path)
    assert len(rows) == 4
    assert rows[:3] == _cluster_rows(now)
    card = rows[3]
    assert card["id"] == result["card_id"]
    assert card["id"].startswith("idxcard-")
    assert card["text"] == "假索引卡：同义词头部 / 核心结论 / 来源 id"
    assert card["title"].startswith("[索引卡]")
    assert "index-card" in card["tags"]
    assert {"python", "rag"} <= set(card["tags"])
    assert card["metadata"]["source_ids"] == ["a1", "a2"]
    assert card["metadata"]["generated_by"] == "compress_knowledge_cluster"
    # prompt 拼了簇内行的 id/text；system_prompt 是索引卡指令
    assert "a1" in captured["prompt"] and "a2" in captured["prompt"]
    assert "索引卡" in captured["system_prompt"]


def test_compress_cluster_below_two_skips_without_llm(tmp_path, monkeypatch):
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", [
        {"id": "b1", "title": "solo", "text": "a lone memory on another topic here",
         "tags": ["other"], "updatedAt": now},
    ])
    before = path.read_bytes()
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False
    ))
    assert res["status"] == "ok"
    assert res["applied"] is False
    assert "no cluster" in res["notes"]
    assert path.read_bytes() == before


def test_compress_cluster_tag_param_selects_cluster(tmp_path, monkeypatch):
    now = int(time.time())
    _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡"))
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a",
        params={"cluster_tag": "python"}, dry_run=False,
    ))
    assert res["status"] == "applied"
    assert res["result"]["cluster_tag"] == "python"
    assert res["result"]["source_ids"] == ["a1", "a2"]


def test_compress_llm_failure_degrades_to_error(tmp_path, monkeypatch):
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    before = path.read_bytes()

    async def _boom(prompt, system_prompt=None):
        raise RuntimeError("llm call failed: gateway unreachable")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _boom)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False
    ))
    assert res["status"] == "error"
    assert res["applied"] is False
    assert path.read_bytes() == before  # LLM 失败 → 文件零改动


# ── compress：幂等 / 取代 / index-card 排除 / 三态决策 ────────────────────────


def _index_card_rows(path: Path) -> list[dict]:
    return [r for r in _read_rows(path) if "index-card" in (r.get("tags") or [])]


def test_compress_idempotent_rerun_skips_without_llm(tmp_path, monkeypatch):
    """同簇无变更连跑两次：第二次 skipped_unchanged、不调 LLM、不新增卡（目标 #1）。"""
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡1"))
    first = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    assert first["status"] == "applied"
    first_card_id = first["result"]["card_id"]
    assert len(_index_card_rows(path)) == 1

    # 第二次：LLM 必须不被调用，否则 AssertionError
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    second = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    # Bug 1 回归：顶层 status 必须是 ToolInvokeResponse 合法 Literal（'ok'），
    # 跳过语义放在 result.action（与 supersede 用 result.action='superseded' 一致），
    # 而非顶层 'skipped_unchanged'（那不在 Literal 内 → 响应校验 500）。
    assert second["status"] == "ok"
    assert second["applied"] is False
    assert second["result"]["action"] == "skipped_unchanged"
    assert second["result"]["card_id"] == first_card_id
    assert second["result"]["reindex_required"] is False
    # 仍只一张索引卡、行数不增
    assert len(_index_card_rows(path)) == 1
    assert len(_read_rows(path)) == 4


def test_compress_supersede_on_new_source(tmp_path, monkeypatch):
    """给簇加一行新源 → supersede：active 只剩一张新卡、旧卡移出主文件 + 进
    governance 快照（可逆）、新卡 metadata.supersedes 含旧卡 id（目标 #2）。"""
    now = int(time.time())
    rows = _cluster_rows(now)
    path = _write_rows(tmp_path, "box-a", rows)
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("旧卡"))
    first = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    old_card_id = first["result"]["card_id"]

    # 给该簇（python,rag）加一行新源 → source_ids/指纹变化
    new_rows = _read_rows(path)
    new_rows.append({"id": "a3", "title": "t3",
                     "text": "python rag note three with enough chars",
                     "tags": ["python", "rag"], "updatedAt": now + 1})
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in new_rows)
                    + "\n", encoding="utf-8")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("新卡"))
    second = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    assert second["status"] == "applied"
    assert second["result"]["action"] == "superseded"
    new_card_id = second["result"]["card_id"]
    assert old_card_id in second["result"]["supersedes"]
    assert second["result"]["reindex_required"] is True

    # active jsonl 恰好一张索引卡（新卡），旧卡已移出
    cards = _index_card_rows(path)
    assert len(cards) == 1
    assert cards[0]["id"] == new_card_id
    assert old_card_id not in [c["id"] for c in cards]
    assert old_card_id not in [r.get("id") for r in _read_rows(path)]
    # 旧卡进 governance 快照（可逆退出 search/embed）
    gov_dir = tmp_path / "tasks" / "rag" / "containers" / "box-a" / "governance"
    snaps = list(gov_dir.glob("superseded-cards-*.jsonl"))
    assert snaps, "superseded card snapshot must exist"
    snap_ids = [json.loads(l)["id"]
                for l in snaps[0].read_text(encoding="utf-8").splitlines() if l]
    assert old_card_id in snap_ids


def test_compress_supersede_collapses_multiple_prior_cards(tmp_path, monkeypatch):
    """迁移场景：簇已堆多张老卡（无 cluster_group 字段，仅 idxcard-{group}- 前缀）→
    一次 supersede 全部退役，最终恰一张新卡（risk #2 不假设 prior 恰一张）。"""
    now = int(time.time())
    rows = _cluster_rows(now)
    # 手工注入 2 张旧卡（模拟历史无 cluster_group/fingerprint 字段的近重复卡）
    group = governance_tools._cluster_group("python,rag")
    rows += [
        {"id": f"idxcard-{group}-{now - 10}", "title": "[索引卡] python,rag",
         "text": "老卡1", "tags": ["python", "rag", "index-card"],
         "createdAt": now - 10, "metadata": {"source_ids": ["a1"]}},
        {"id": f"idxcard-{group}-{now - 5}", "title": "[索引卡] python,rag",
         "text": "老卡2", "tags": ["python", "rag", "index-card"],
         "createdAt": now - 5, "metadata": {"source_ids": ["a1", "a2"]}},
    ]
    path = _write_rows(tmp_path, "box-a", rows)
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("合并新卡"))
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    assert res["status"] == "applied"
    assert res["result"]["action"] == "superseded"
    assert res["result"]["superseded_count"] == 2
    cards = _index_card_rows(path)
    assert len(cards) == 1  # 多张旧卡全退役，恰留一张新卡
    assert cards[0]["id"] == res["result"]["card_id"]


def test_compress_inplace_edit_triggers_supersede_not_skip(tmp_path, monkeypatch):
    """源 id 集不变但原地编辑某行 text+updatedAt → 指纹变 → supersede（非 skip）。"""
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡v1"))
    _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))

    # 原地编辑 a1 的 text + updatedAt（id 集不变）
    edited = _read_rows(path)
    for r in edited:
        if r.get("id") == "a1":
            r["text"] = "python rag note ONE edited in place with new content"
            r["updatedAt"] = now + 100
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in edited)
                    + "\n", encoding="utf-8")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡v2"))
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    assert res["status"] == "applied"
    assert res["result"]["action"] == "superseded"
    assert len(_index_card_rows(path)) == 1


def test_compress_never_resummarizes_index_card_rows(tmp_path, monkeypatch):
    """簇里混入一张已有 index-card 行 → 它不进 cluster.source_ids、不喂 LLM（目标 #3）。"""
    now = int(time.time())
    rows = _cluster_rows(now)
    # 混入一张已有索引卡，且其 tags 含簇 tag（python/rag）——巧合排除会失效，
    # 显式排除必须把它挡在簇外。
    rows.append({"id": "idxcard-prev-1", "title": "[索引卡] python,rag",
                 "text": "前一张卡正文不应被二次总结",
                 "tags": ["python", "rag", "index-card"], "createdAt": now,
                 "metadata": {"source_ids": ["a1", "a2"]}})
    _write_rows(tmp_path, "box-a", rows)
    captured: dict = {}

    async def _fake(prompt, system_prompt=None):
        captured.setdefault("prompts", []).append(prompt)
        return "新卡"

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _fake)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a",
        params={"cluster_tag": "python"}, dry_run=False))
    assert res["status"] == "applied"
    # 索引卡行不在源集
    assert "idxcard-prev-1" not in res["result"]["source_ids"]
    assert set(res["result"]["source_ids"]) == {"a1", "a2"}
    # 喂 LLM 的 prompt 不含那张卡的 id / 正文
    all_prompts = "\n".join(captured.get("prompts", []))
    assert "idxcard-prev-1" not in all_prompts
    assert "不应被二次总结" not in all_prompts


def test_compress_dry_run_three_state_decisions(tmp_path, monkeypatch):
    """dry_run 预览三态决策正确：first_card → skip_unchanged → supersede（目标 #4）。"""
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)

    # ① 首卡前：decision=first_card
    p1 = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=True))
    assert p1["result"]["plan"]["decision"] == "first_card"

    # 产一张卡（真执行）
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡"))
    first = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    card_id = first["result"]["card_id"]

    # ② 无变更：decision=skip_unchanged + 既有 card_id
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    p2 = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=True))
    assert p2["result"]["plan"]["decision"] == "skip_unchanged"
    assert p2["result"]["plan"]["existing_card_id"] == card_id
    assert p2["result"]["plan"]["new_source_count"] == 0

    # ③ 加一行新源：decision=supersede(card X) + 自上次以来 +1 源
    new_rows = _read_rows(path)
    new_rows.append({"id": "a3", "title": "t3",
                     "text": "python rag note three with enough chars",
                     "tags": ["python", "rag"], "updatedAt": now + 1})
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in new_rows)
                    + "\n", encoding="utf-8")
    p3 = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=True))
    assert p3["result"]["plan"]["decision"] == "supersede"
    assert card_id in p3["result"]["plan"]["superseded_card_ids"]
    assert p3["result"]["plan"]["new_source_count"] == 1


def test_compress_idempotent_flag_off_falls_back_to_append(tmp_path, monkeypatch):
    """两开关置 False → 退回无脑追加：同簇连跑两次堆两张卡（灰度/回退路径）。"""
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setenv("TM_COMPRESS_IDEMPOTENT", "0")
    monkeypatch.setenv("TM_COMPRESS_SUPERSEDE", "0")
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡"))
    _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    second = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))
    assert second["status"] == "applied"
    assert second["result"]["action"] == "first_card"
    # 旧 append 行为：堆两张索引卡
    assert len(_index_card_rows(path)) == 2


# ── 活栈实测回归：Bug 1（跳过响应模型合法）/ Bug 2（历史孤儿）/ 廉价合并 ──────


def test_skip_path_response_passes_tool_invoke_response_model(tmp_path, monkeypatch):
    """Bug 1 回归（活栈实测 500）：「已有 1 张指纹匹配 active 卡」跑 compress →
    返回体必须能通过 ToolInvokeResponse 校验（顶层 status ∈ 合法 Literal、非
    'skipped_unchanged'），否则真实端点序列化时 ValidationError → 500。

    单测过去全绿却漏抓，因从不把返回 dict 推过响应模型——本例显式校验。"""
    now = int(time.time())
    _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning("卡1"))
    _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    skip = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))

    # 顶层 status 必须在合法集内（这正是端点 500 的根因字段）。
    legal = {"ok", "disabled", "dry_run", "error", "deferred", "applied"}
    assert skip["status"] in legal
    assert skip["status"] == "ok"
    # 真正经过响应模型校验——不抛 = 端点不会 500。
    validated = ToolInvokeResponse(**skip)
    assert validated.status == "ok"
    assert validated.applied is False
    # 跳过语义落在 result.action，且未新增卡。
    assert skip["result"]["action"] == "skipped_unchanged"


def test_consolidate_collapses_history_orphan_via_source_ids(tmp_path, monkeypatch):
    """Bug 2 回归（活栈 idxcard-39190d8e 漏抓）：同簇 N 张老卡，部分无 fingerprint /
    无 cluster_group、id 前缀 group 哈希不同，但 metadata.source_ids 相同 → 按
    source_ids 兜底匹配全部识别为该簇的卡，终态 active 索引卡恰一张、其余进快照。"""
    now = int(time.time())
    rows = _cluster_rows(now)
    group = governance_tools._cluster_group("python,rag")
    fp = governance_tools._cluster_fingerprint(
        [r for r in rows if r["id"] in ("a1", "a2")])
    # ① 一张「当前」匹配卡：正确 group + fingerprint（active）。
    rows.append({
        "id": f"idxcard-{group}-{fp[:8]}-{now - 20}",
        "title": "[索引卡] python,rag", "text": "当前匹配卡",
        "tags": ["python", "rag", "index-card"], "createdAt": now - 20,
        "metadata": {"source_ids": ["a1", "a2"], "cluster_group": group,
                     "cluster_fingerprint": fp, "status": "active"},
    })
    # ② 历史孤儿卡：group 哈希不同（不同时期 tag 串）、无 fingerprint、无 cluster_group，
    #    但 source_ids 同 → 旧 group/前缀匹配抓不到，须靠 source_ids 兜底。
    rows.append({
        "id": "idxcard-39190d8e-1781511724",
        "title": "[索引卡] 旧 tag 串", "text": "历史孤儿卡",
        "tags": ["python", "rag", "index-card"], "createdAt": now - 100,
        "metadata": {"source_ids": ["a1", "a2"]},
    })
    path = _write_rows(tmp_path, "box-a", rows)

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))

    assert res["status"] == "applied"
    assert res["result"]["action"] == "consolidated"
    assert res["result"]["reindex_required"] is False
    # 终态：active 索引卡恰一张（保留的匹配卡）。
    cards = _index_card_rows(path)
    assert len(cards) == 1
    assert cards[0]["metadata"].get("cluster_fingerprint") == fp
    # 历史孤儿被退役 → 移出 active，进 governance 快照（可逆，不硬删）。
    assert "idxcard-39190d8e-1781511724" not in [c["id"] for c in cards]
    assert "idxcard-39190d8e-1781511724" not in [r.get("id") for r in _read_rows(path)]
    gov_dir = tmp_path / "tasks" / "rag" / "containers" / "box-a" / "governance"
    snaps = list(gov_dir.glob("superseded-cards-*.jsonl"))
    assert snaps, "retired orphan must land in governance snapshot"
    snap_ids = [json.loads(l)["id"]
                for l in snaps[0].read_text(encoding="utf-8").splitlines() if l]
    assert "idxcard-39190d8e-1781511724" in snap_ids


def test_consolidate_does_not_call_llm(tmp_path, monkeypatch):
    """廉价合并不调 LLM：有匹配卡 + 多余孤儿时，LLM mock 断言未被调用，
    孤儿被移出、匹配卡保留（内容没变 → 不重新总结）。"""
    now = int(time.time())
    rows = _cluster_rows(now)
    group = governance_tools._cluster_group("python,rag")
    fp = governance_tools._cluster_fingerprint(
        [r for r in rows if r["id"] in ("a1", "a2")])
    match_id = f"idxcard-{group}-{fp[:8]}-{now - 20}"
    rows += [
        {"id": match_id, "title": "[索引卡]", "text": "匹配卡",
         "tags": ["python", "rag", "index-card"], "createdAt": now - 20,
         "metadata": {"source_ids": ["a1", "a2"], "cluster_group": group,
                      "cluster_fingerprint": fp, "status": "active"}},
        {"id": "idxcard-deadbeef-111", "title": "[索引卡] 旧", "text": "孤儿",
         "tags": ["python", "rag", "index-card"], "createdAt": now - 50,
         "metadata": {"source_ids": ["a1", "a2"]}},
    ]
    path = _write_rows(tmp_path, "box-a", rows)

    # LLM 一旦被调用即 AssertionError —— 廉价合并路径绝不触发总结。
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=False))

    assert res["status"] == "applied"
    assert res["result"]["action"] == "consolidated"
    assert res["result"]["card_id"] == match_id
    assert "idxcard-deadbeef-111" in res["result"]["supersedes"]
    cards = _index_card_rows(path)
    assert len(cards) == 1 and cards[0]["id"] == match_id
    assert "idxcard-deadbeef-111" not in [r.get("id") for r in _read_rows(path)]
    # 响应仍过模型校验（顶层 'applied' 合法）。
    assert ToolInvokeResponse(**res).status == "applied"


# ── snapshot_and_quarantine：sidecar 文件落点补充 ─────────────────────────────


def test_quarantine_sidecar_files_land_in_governance_dir(tmp_path):
    now = int(time.time())
    _write_rows(tmp_path, "box-a", [
        {"id": "stale-1", "text": "an old but reasonably long memory text",
         "updatedAt": now - 200 * 86_400},
        {"id": "healthy-1", "text": "a fresh memory with plenty of content here",
         "updatedAt": now - 3_600},
    ])
    res = _run(governance_tools.invoke_tool(
        "snapshot_and_quarantine", container="box-a", dry_run=False
    ))
    assert res["status"] == "applied"
    gov_dir = tmp_path / "tasks" / "rag" / "containers" / "box-a" / "governance"
    snapshot = Path(res["result"]["snapshot_path"])
    quarantine = Path(res["result"]["quarantine_path"])
    assert snapshot.parent == gov_dir and quarantine.parent == gov_dir
    assert snapshot.name.startswith("snapshot-") and snapshot.suffix == ".jsonl"
    assert quarantine.name.startswith("quarantine-") and quarantine.suffix == ".jsonl"
    # 原子写不留 tmp 残骸
    assert not list(gov_dir.glob("*.tmp"))


# ── tune_model_parameters：LLM 调参带护栏 ─────────────────────────────────────


def _invoke_tune(dry_run=False):
    return _run(governance_tools.invoke_tool(
        "tune_model_parameters", container="box-a", dry_run=dry_run
    ))


def test_tune_not_dry_run_applies_valid_suggestion(tmp_path, monkeypatch):
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning(json.dumps({
        "similarity_threshold": 0.42, "default_topk": 8,
        "citation_enabled": True, "rationale": "tighter threshold",
    })))
    res = _invoke_tune()
    assert res["status"] == "applied"
    assert res["applied"] is True
    result = res["result"]
    assert sorted(result["applied_keys"]) == [
        "citation_enabled", "default_topk", "similarity_threshold",
    ]
    assert result["rejected"] == []
    assert result["rationale"] == "tighter threshold"
    assert result["after"]["similarity_threshold"] == 0.42
    # 经 config_store 真持久化（与 PUT /admin/config 同读路径可读回）
    assert config_store.get_cached("config:rag:similarity_threshold", None) == 0.42
    assert config_store.get_cached("config:rag:default_topk", None) == 8
    assert config_store.get_cached("config:rag:citation_enabled", None) is True


def test_tune_rejects_out_of_range_and_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning(json.dumps({
        "similarity_threshold": 5.0,        # 越界 [0,1]
        "default_topk": 500,                # 越界 [1,50]
        "citation_enabled": "yes",          # 类型不符（必须 bool）
        "embedding_dim": 1024,              # 不在 allow-list
        "rationale": "bad suggestion",
    })))
    res = _invoke_tune()
    assert res["status"] == "ok"
    assert res["applied"] is False  # 全被拒
    rejected = {r["key"]: r["reason"] for r in res["result"]["rejected"]}
    assert rejected["similarity_threshold"] == "out_of_range_or_bad_type"
    assert rejected["default_topk"] == "out_of_range_or_bad_type"
    assert rejected["citation_enabled"] == "out_of_range_or_bad_type"
    assert rejected["embedding_dim"] == "not_in_allow_list"
    assert res["result"]["applied_keys"] == []
    # 越界键一个都没写进 config
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None
    assert config_store.get_cached("config:rag:default_topk", None) is None


def test_tune_partial_apply_only_valid_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_returning(json.dumps({
        "similarity_threshold": 0.3, "default_topk": 999,
        "citation_enabled": None, "rationale": "mixed",
    })))
    res = _invoke_tune()
    assert res["status"] == "applied"
    assert res["applied"] is True
    assert res["result"]["applied_keys"] == ["similarity_threshold"]
    assert [r["key"] for r in res["result"]["rejected"]] == ["default_topk"]
    assert config_store.get_cached("config:rag:similarity_threshold", None) == 0.3
    assert config_store.get_cached("config:rag:default_topk", None) is None


def test_tune_non_json_output_errors_config_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(governance_tools, "_llm_oneshot",
                        _llm_returning("I think you should lower the threshold."))
    res = _invoke_tune()
    assert res["status"] == "error"
    assert res["applied"] is False
    assert res["result"]["error"] == "llm_output_not_json"
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None
    assert config_store.get_cached("config:rag:citation_enabled", None) is None


# ── dry_run=True：三工具仍只产 plan、零数据改动 ───────────────────────────────


def test_dry_run_true_three_tools_do_not_touch_data(tmp_path, monkeypatch):
    now = int(time.time())
    path = _write_rows(tmp_path, "box-a", [
        {"id": "stale-1", "text": "an old but reasonably long memory text",
         "updatedAt": now - 200 * 86_400},
        *_cluster_rows(now),
    ])
    before = path.read_bytes()
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    for tool in ("snapshot_and_quarantine", "compress_knowledge_cluster",
                 "tune_model_parameters"):
        res = _run(governance_tools.invoke_tool(tool, container="box-a", dry_run=True))
        assert res["status"] == "dry_run"
        assert res["applied"] is False
        assert "dry_run preview" in res["notes"]
        assert res["result"]["plan"]["would_execute"] is False
    assert path.read_bytes() == before  # 主文件零改动
    gov_dir = tmp_path / "tasks" / "rag" / "containers" / "box-a" / "governance"
    assert not gov_dir.exists()  # 无 sidecar 落盘
    assert config_store.get_cached("config:rag:similarity_threshold", None) is None


def test_dry_run_previews_carry_real_scan(tmp_path, monkeypatch):
    """dry_run 预览须是真实只读扫描而非空回声：compress 给目标簇、tune 给信号。

    回归 2026-06-11 用户实测「试运行返回 container=null/params_echo={} 空 plan」
    —— 修复后即便填了容器，compress/tune 的 dry_run 也带可执行预览（不调 LLM）。"""
    now = int(time.time())
    _write_rows(tmp_path, "box-a", _cluster_rows(now))
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)

    comp = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="box-a", dry_run=True))
    cplan = comp["result"]["plan"]
    assert comp["status"] == "dry_run"
    assert cplan["container"] == "box-a"
    assert cplan["would_compress"] is True
    assert cplan["cluster_size"] == 2
    assert set(cplan["source_ids"]) == {"a1", "a2"}

    tune = _run(governance_tools.invoke_tool(
        "tune_model_parameters", container="box-a", dry_run=True))
    tplan = tune["result"]["plan"]
    assert tune["status"] == "dry_run"
    assert tplan["signals"]["object_count"] == 3
    assert "current_config" in tplan["signals"]
    assert "similarity_threshold" in tplan["tunable_keys"]


def test_dry_run_preview_no_container_is_graceful(monkeypatch):
    """无容器时预览不抛、给出 container required 提示（前端已拦截，后端兜底）。"""
    monkeypatch.setattr(governance_tools, "_llm_oneshot", _llm_must_not_be_called)
    comp = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container=None, dry_run=True))
    assert comp["status"] == "dry_run"
    assert comp["result"]["plan"]["would_compress"] is False
    assert comp["result"]["plan"]["cluster_size"] == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
