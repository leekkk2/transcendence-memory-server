"""Blueprint P6 单测：治理工具箱 registry + 容器×工具激活解析（Agent B 交付 §3）。

纯逻辑，不依赖 fastapi / lancedb / 真 Redis —— 只 import
scripts/{config_store,governance_tools,token_meter,redis_client}（均 import-safe，
Redis 通过 TM_REDIS_ENABLED=0 + monkeypatch 关掉）。覆盖：

  * resolve_tool_enabled 的容器覆盖 / 全局回退 / 新工具默认；
  * resolve_matrix 结构（resolved_map 容器覆盖全局 + raw_map 继承时为 None）；
  * invoke 被禁用工具 → status='disabled' 不动数据；
  * manage_token_quotas 读 P3 token 数据（mock token_meter）；
  * 破坏性 / LLM 工具 dry_run 不改数据 / deferred；
  * update_container_routing 加性 merge 不覆盖其它容器。

conftest.py 在 collection 期 import fastapi（slim 环境无），故本套件设计为
**独立可跑**（隔离 venv 注入 scripts/ 到 sys.path，按 P4/P6-A 隔离法）。
"""
from __future__ import annotations

import asyncio
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
import token_meter  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch, tmp_path):
    """Fresh WORKSPACE + reset config singleton per test so the config DB is
    isolated and no override bleeds across cases."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()
    yield
    config_store.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


def _fake_cfg(table):
    def _get(key, default=None):
        return table.get(key, default)

    return _get


async def _async_none(key, default=None):
    return None


# ── resolve_tool_enabled: container override / global fallback / new default ──


def test_resolve_global_default_all_on(monkeypatch):
    # No overrides at all → global map defaults to all preset tools on.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({}))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    assert _run(governance_tools.resolve_tool_enabled("update_container_routing", "alpha")) is True
    assert _run(governance_tools.resolve_tool_enabled("manage_token_quotas")) is True


def test_resolve_global_map_override(monkeypatch):
    # Global map turns one tool off → resolves off when no container override.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"snapshot_and_quarantine": False},
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    assert _run(governance_tools.resolve_tool_enabled("snapshot_and_quarantine", "alpha")) is False


def test_resolve_container_overrides_global(monkeypatch):
    # Global says ON; container map says OFF → container override wins.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"update_container_routing": True},
    }))

    async def _cfg_get(key, default=None):
        if key == "config:tools:container:alpha:enabled_map":
            return '{"update_container_routing": false}'
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _cfg_get)
    assert _run(governance_tools.resolve_tool_enabled("update_container_routing", "alpha")) is False
    # A different container with no override inherits the global ON.
    assert _run(governance_tools.resolve_tool_enabled("update_container_routing", "beta")) is True


def test_resolve_new_tool_falls_back_to_new_default(monkeypatch):
    # A tool name absent from the global map → new_tool_default_enabled (False).
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {},  # empty → tool not present
        "config:tools:new_tool_default_enabled": False,
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    # use a real tool name but with an empty global map → not present → new default
    assert _run(governance_tools.resolve_tool_enabled("manage_token_quotas")) is False


def test_resolve_container_scoped_tool_ignores_container_for_global_scope(monkeypatch):
    # manage_token_quotas is scope=global → container override map is ignored.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"manage_token_quotas": True},
    }))

    async def _cfg_get(key, default=None):
        if key == "config:tools:container:alpha:enabled_map":
            return '{"manage_token_quotas": false}'
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _cfg_get)
    # Even with a container override turning it off, global-scope tool stays on.
    assert _run(governance_tools.resolve_tool_enabled("manage_token_quotas", "alpha")) is True


# ── resolve_matrix: structure (resolved_map / raw_map) ───────────────────────


def test_resolve_matrix_inherit_and_override(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {n: True for n in governance_tools.TOOLS},
    }))

    async def _cfg_get(key, default=None):
        if key == "config:tools:container:alpha:enabled_map":
            return '{"snapshot_and_quarantine": false}'
        return None

    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _cfg_get)
    matrix = _run(governance_tools.resolve_matrix(["alpha", "beta"]))
    by_c = {m["container"]: m for m in matrix}
    # alpha has a raw override blob; its resolved snapshot tool is off, rest on.
    assert by_c["alpha"]["raw_map"] == {"snapshot_and_quarantine": False}
    assert by_c["alpha"]["resolved_map"]["snapshot_and_quarantine"] is False
    assert by_c["alpha"]["resolved_map"]["update_container_routing"] is True
    # beta has no override → raw_map None, fully inherits global (all on).
    assert by_c["beta"]["raw_map"] is None
    assert all(by_c["beta"]["resolved_map"].values())
    # Every preset tool is present in the resolved map.
    assert set(by_c["beta"]["resolved_map"]) == set(governance_tools.TOOLS)


# ── invoke: disabled tool → status='disabled' no-op ──────────────────────────


def test_invoke_disabled_tool(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"snapshot_and_quarantine": False},
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool("snapshot_and_quarantine", container="alpha"))
    assert res["status"] == "disabled"
    assert res["applied"] is False


def test_invoke_unknown_tool(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({}))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool("no_such_tool", container="alpha"))
    assert res["status"] == "error"
    assert res["applied"] is False


# ── invoke: manage_token_quotas reads P3 data (mock token_meter) ─────────────


def test_invoke_manage_token_quotas_reads_p3(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"manage_token_quotas": True},
        "config:token:daily_budget": 1000,
        "config:token:hourly_budget": 200,
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)

    async def _live(agent_ids):
        return {"agent-1": 350}

    async def _over(agent_id, model):
        return {"over": True, "scope": "daily", "fallback_model": "cheap"}

    async def _window_total(prefix, stamp, agent_id):
        return 90

    monkeypatch.setattr(token_meter, "live_today_totals", _live)
    monkeypatch.setattr(token_meter, "over_budget", _over)
    monkeypatch.setattr(token_meter, "_window_total", _window_total)

    res = _run(governance_tools.invoke_tool(
        "manage_token_quotas", params={"agent_id": "agent-1"}
    ))
    assert res["status"] == "ok"
    assert res["applied"] is False  # read-only, never mutates
    result = res["result"]
    assert result["agent_id"] == "agent-1"
    assert result["daily_used"] == 350
    assert result["daily_budget"] == 1000
    assert result["hourly_used"] == 90
    assert result["hourly_budget"] == 200
    assert result["mode_suggestion"] == "fallback"


# ── invoke: latency tool honest deferred ─────────────────────────────────────


def test_invoke_analyze_latency_deferred(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"analyze_retrieval_latency": True},
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool("analyze_retrieval_latency", container="alpha"))
    assert res["status"] == "deferred"
    assert res["applied"] is False
    assert res["result"]["latency_metrics"] is None  # honest, not fabricated


# ── invoke: destructive / LLM tools are report-only (dry_run / deferred) ──────


def test_invoke_destructive_dry_run_does_not_apply(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"snapshot_and_quarantine": True},
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool(
        "snapshot_and_quarantine", container="alpha", dry_run=True
    ))
    assert res["status"] == "dry_run"
    assert res["applied"] is False
    assert res["result"]["plan"]["would_execute"] is False


def test_invoke_llm_tool_not_dry_run_is_deferred(monkeypatch):
    # dry_run=False on an LLM tool → still report-only (deferred), never executes.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:tools:global_enabled_map": {"compress_knowledge_cluster": True},
    }))
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool(
        "compress_knowledge_cluster", container="alpha", dry_run=False
    ))
    assert res["status"] == "deferred"
    assert res["applied"] is False


# ── invoke: update_container_routing additive merge ──────────────────────────


def test_invoke_update_routing_additive_merge(monkeypatch):
    # Seed existing routing for another container; the tool must preserve it and
    # only add/replace the target container's entry.
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    # Pre-existing routing_rules with container 'beta' already configured.
    _run(config_store.set("config:container:routing_rules", {"beta": {"weight": 2}}))

    res = _run(governance_tools.invoke_tool(
        "update_container_routing",
        container="alpha",
        params={"rules": {"weight": 5}},
        dry_run=False,
    ))
    assert res["status"] == "ok"
    assert res["applied"] is True
    merged = res["result"]["merged_routing_rules"]
    # additive: beta preserved, alpha added.
    assert merged["beta"] == {"weight": 2}
    assert merged["alpha"] == {"weight": 5}

    # And the persisted config reflects the merge (read back through get_cached).
    persisted = config_store.get_cached("config:container:routing_rules", {})
    assert persisted["beta"] == {"weight": 2}
    assert persisted["alpha"] == {"weight": 5}


def test_invoke_update_routing_params_dry_run_does_not_persist(monkeypatch):
    # OR semantics, params side: top-level dry_run=False but params.dry_run=True
    # → still a preview only (the conservative side wins, nothing is written).
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    _run(config_store.set("config:container:routing_rules", {"beta": {"weight": 2}}))
    res = _run(governance_tools.invoke_tool(
        "update_container_routing",
        container="alpha",
        params={"rules": {"weight": 9}, "dry_run": True},
        dry_run=False,
    ))
    assert res["status"] == "dry_run"
    assert res["applied"] is False
    # Persisted store still only has beta — alpha was a preview only.
    persisted = config_store.get_cached("config:container:routing_rules", {})
    assert "alpha" not in persisted
    assert persisted["beta"] == {"weight": 2}


def test_invoke_update_routing_top_level_dry_run_default_does_not_persist(monkeypatch):
    # OR semantics, top-level side + endpoint default: invoke_tool's dry_run
    # defaults to True (the POST contract default). With params carrying NO
    # dry_run, the top-level default must STILL prevent a real write — the
    # contract bypass this fix closes (previously _invoke_update_container_routing
    # only read params.dry_run and silently persisted on the default body).
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    _run(config_store.set("config:container:routing_rules", {"beta": {"weight": 2}}))
    res = _run(governance_tools.invoke_tool(
        "update_container_routing",
        container="alpha",
        params={"rules": {"weight": 7}},  # NO dry_run in params; rely on default
    ))
    assert res["status"] == "dry_run"
    assert res["applied"] is False
    persisted = config_store.get_cached("config:container:routing_rules", {})
    assert "alpha" not in persisted
    assert persisted["beta"] == {"weight": 2}


def test_invoke_update_routing_requires_container(monkeypatch):
    monkeypatch.setattr(governance_tools.redis_client, "cfg_get", _async_none)
    res = _run(governance_tools.invoke_tool(
        "update_container_routing", container=None, params={"rules": {"weight": 1}}
    ))
    assert res["status"] == "error"
    assert res["applied"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
