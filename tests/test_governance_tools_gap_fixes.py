"""蓝图验收缺口修复单测（G2 / G4 — governance_tools）。

* G2 analyze_retrieval_latency：不再 honest-deferred —— 接上 usage analytics
  （/admin/usage/endpoints 同源聚合）。注入假 usage 行到临时 queue.db，断言
  /search、/query 的 calls/p50_ms/p95_ms 聚合正确；窗口过滤 / 非法窗口回退。
* G4 snapshot_and_quarantine：plan 携带目标容器真实 memory_objects.jsonl 的
  只读候选扫描（超龄 / 超短启发式），would_execute 恒 False，dry_run 真假
  都只产 plan、绝不动数据。

与 test_governance_tools_p6.py 同款隔离法：scripts/ 注入 sys.path、
TM_REDIS_ENABLED=0、每例独立 WORKSPACE。G2 聚合用例经 usage_analytics 间接
依赖 fastapi（与 conftest 一致，标准 venv 可跑）。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
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
    """独立 WORKSPACE + 重置 config 单例，避免用例间状态串扰。"""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()
    yield
    config_store.reset_for_tests()


@pytest.fixture(autouse=True)
def _all_tools_enabled(monkeypatch):
    """开关全开 + Redis 关闭，让 invoke 直达 handler。"""
    monkeypatch.setattr(
        config_store, "get_cached",
        lambda key, default=None: {
            "config:tools:global_enabled_map": {n: True for n in governance_tools.TOOLS},
        }.get(key, default),
    )

    async def _none(key, default=None):
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _none)


def _run(coro):
    return asyncio.run(coro)


# ── G2 helpers：往临时 queue.db 注入假 usage 行 ───────────────────────────────


def _seed_usage_rows(tmp_path: Path, rows: list[tuple[str, int, int]]) -> Path:
    """rows = [(path, latency_ms, ts_ms)] → WORKSPACE/tasks/rag/queue.db。"""
    import usage_analytics

    db = tmp_path / "tasks" / "rag" / "queue.db"
    usage_analytics.ensure_schema(db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO api_request_log "
            "(ts, method, path, status, latency_ms, container) "
            "VALUES (?, 'POST', ?, 200, ?, 'box-a')",
            [(ts, path, lat) for path, lat, ts in rows],
        )
        conn.commit()
    return db


def _invoke_latency(params=None):
    return _run(governance_tools.invoke_tool(
        "analyze_retrieval_latency", container="box-a", params=params or {}
    ))


# ── G2：聚合正确性 ────────────────────────────────────────────────────────────


def test_latency_aggregates_injected_usage_rows(tmp_path):
    now_ms = int(time.time() * 1000)
    db = _seed_usage_rows(tmp_path, [
        # /search 五条已知分布：p50=30，p95=100（usage_analytics._percentile 语义）
        *[("/search", lat, now_ms - 1_000) for lat in (10, 20, 30, 40, 100)],
        ("/query", 50, now_ms - 1_000),
        ("/query", 70, now_ms - 1_000),
        # 非检索端点不得进入 latency_metrics.endpoints
        ("/ingest-memory/objects", 999, now_ms - 1_000),
    ])
    res = _invoke_latency()
    assert res["status"] == "ok"
    assert res["applied"] is False
    metrics = res["result"]["latency_metrics"]
    assert set(metrics["endpoints"]) == {"/search", "/query"}
    assert metrics["endpoints"]["/search"] == {"calls": 5, "p50_ms": 30, "p95_ms": 100}
    assert metrics["endpoints"]["/query"] == {"calls": 2, "p50_ms": 50, "p95_ms": 70}
    # 粒度做不到 per-container → notes 注明 global
    assert "global" in res["notes"]
    # 只读：注入的 8 行原样还在
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_request_log").fetchone()[0] == 8


def test_latency_window_filters_old_rows_and_bad_window_falls_back(tmp_path):
    now_ms = int(time.time() * 1000)
    _seed_usage_rows(tmp_path, [
        ("/search", 30, now_ms - 40 * 86_400 * 1000),  # 40 天前 → 24h 窗口外
    ])
    res = _invoke_latency(params={"window": "24h"})
    metrics = res["result"]["latency_metrics"]
    assert metrics["endpoints"]["/search"] == {"calls": 0, "p50_ms": 0, "p95_ms": 0}

    res2 = _invoke_latency(params={"window": "bogus"})
    assert res2["status"] == "ok"
    assert res2["result"]["latency_metrics"]["window"] == "7d"  # 非法窗口回退默认


def test_latency_no_usage_db_reports_ok_empty(tmp_path):
    # 直接调 handler：invoke_tool 的开关解析层会经 config_store 创建 queue.db，
    # 这里要验证的是 handler 本身在无库时只读（不为报告创建文件）。
    res = governance_tools._invoke_analyze_retrieval_latency("box-a", {})
    assert res["status"] == "ok"
    assert res["result"]["latency_metrics"]["endpoints"] == {}
    assert not (tmp_path / "tasks" / "rag" / "queue.db").exists()


def test_latency_db_present_but_no_rows_reports_zero_calls(tmp_path):
    _seed_usage_rows(tmp_path, [])  # 建库不注入行
    res = _invoke_latency()
    assert res["status"] == "ok"
    endpoints = res["result"]["latency_metrics"]["endpoints"]
    assert endpoints["/search"]["calls"] == 0
    assert endpoints["/query"]["calls"] == 0


# ── G4 helpers：临时容器 memory_objects.jsonl fixture ─────────────────────────


def _write_container_rows(tmp_path: Path, container: str, rows: list[dict]) -> tuple[Path, dict]:
    """写入真实 JSONL；返回 (路径, id→该行 utf-8 字节数(含换行))。"""
    root = tmp_path / "tasks" / "rag" / "containers" / container
    root.mkdir(parents=True)
    path = root / "memory_objects.jsonl"
    sizes: dict[str, int] = {}
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False) + "\n"
            sizes[row["id"]] = len(line.encode("utf-8"))
            fh.write(line)
    return path, sizes


def _invoke_quarantine(container="box-a", dry_run=True, params=None):
    return _run(governance_tools.invoke_tool(
        "snapshot_and_quarantine", container=container, params=params or {},
        dry_run=dry_run,
    ))


# ── G4：plan 候选扫描来自真实文件，且恒不执行 ─────────────────────────────────


def test_quarantine_plan_scans_real_container_rows(tmp_path):
    now = int(time.time())
    stale_ts = now - 200 * 86_400  # 超过默认 180 天
    _, sizes = _write_container_rows(tmp_path, "box-a", [
        {"id": "stale-1", "text": "an old but reasonably long memory text",
         "updatedAt": stale_ts},
        {"id": "short-1", "text": "hi", "updatedAt": now - 3_600},
        {"id": "healthy-1", "text": "a fresh memory with plenty of content here",
         "updatedAt": now - 3_600},
    ])
    res = _invoke_quarantine(dry_run=True)
    assert res["status"] == "dry_run"
    assert res["applied"] is False
    plan = res["result"]["plan"]
    assert plan["would_execute"] is False
    assert plan["scanned_count"] == 3
    by_id = {c["document_id"]: c for c in plan["candidates"]}
    assert set(by_id) == {"stale-1", "short-1"}  # healthy 行不在候选里
    assert any("stale" in r for r in by_id["stale-1"]["reasons"])
    assert by_id["stale-1"]["last_updated"] == stale_ts
    assert any("short" in r for r in by_id["short-1"]["reasons"])
    # 字节数来自真实文件行（含换行）
    assert by_id["stale-1"]["bytes"] == sizes["stale-1"]
    assert by_id["short-1"]["bytes"] == sizes["short-1"]


def test_quarantine_not_dry_run_still_plan_only_and_data_untouched(tmp_path):
    now = int(time.time())
    path, _ = _write_container_rows(tmp_path, "box-a", [
        {"id": "stale-1", "text": "an old but reasonably long memory text",
         "updatedAt": now - 200 * 86_400},
    ])
    before = path.read_bytes()
    res = _invoke_quarantine(dry_run=False)
    # dry_run=False 同样只产 plan（执行开关 P6 关）—— 蓝图语义不变
    assert res["status"] == "deferred"
    assert res["applied"] is False
    plan = res["result"]["plan"]
    assert plan["would_execute"] is False
    assert [c["document_id"] for c in plan["candidates"]] == ["stale-1"]
    assert path.read_bytes() == before  # 真实文件零改动


def test_quarantine_corrupt_lines_skipped_not_counted(tmp_path):
    root = tmp_path / "tasks" / "rag" / "containers" / "box-a"
    root.mkdir(parents=True)
    (root / "memory_objects.jsonl").write_text(
        '{"id":"good-1","text":"hi"}\nnot-valid-json\n\n', encoding="utf-8"
    )
    plan = _invoke_quarantine()["result"]["plan"]
    assert plan["scanned_count"] == 1  # 坏行 / 空行不计入
    assert [c["document_id"] for c in plan["candidates"]] == ["good-1"]


def test_quarantine_missing_store_yields_empty_scan(tmp_path):
    plan = _invoke_quarantine(container="never-created")["result"]["plan"]
    assert plan["would_execute"] is False
    assert plan["scanned_count"] == 0
    assert plan["candidates"] == []


def test_quarantine_without_container_notes_scan_skipped():
    plan = _invoke_quarantine(container=None)["result"]["plan"]
    assert plan["would_execute"] is False
    assert plan["candidates"] == []
    assert "container required" in plan["scan_notes"]


def test_quarantine_respects_max_age_days_param(tmp_path):
    now = int(time.time())
    _write_container_rows(tmp_path, "box-a", [
        {"id": "mid-age", "text": "a memory long enough to dodge the short rule",
         "updatedAt": now - 40 * 86_400},
    ])
    # 默认 180 天 → 不算超龄
    assert _invoke_quarantine()["result"]["plan"]["candidates"] == []
    # 收紧到 30 天 → 命中超龄启发式
    plan = _invoke_quarantine(params={"max_age_days": 30})["result"]["plan"]
    assert [c["document_id"] for c in plan["candidates"]] == ["mid-age"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
