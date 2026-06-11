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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
