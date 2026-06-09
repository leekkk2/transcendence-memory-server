"""Blueprint P6 单测：梦境引擎 + 治理产出存储（Agent A 交付 §5）。

纯逻辑，不依赖 fastapi / lancedb / 真 Redis —— 只 import
scripts/{config_store,dreaming,governance_store,redis_client}（均 import-safe，
Redis 通过 TM_REDIS_ENABLED=0 + monkeypatch 关掉）。覆盖：

  * resolve_container_dream_config 的全局/容器级覆盖与继承；
  * global_enabled=false → run_dream_cycle 跳过、不触任何动作；
  * dry_run report-only：破坏性动作 applied=False、不删数据；
  * governance_store 写读 round-trip 且强制带 exclude_from_rag/system_type；
  * scheduler 在 scheduler_enabled=false 时不启动（行为保持）。

conftest.py 在 collection 期 import fastapi（slim 环境无），故本套件设计为
**独立可跑**（隔离 venv 注入 scripts/ 到 sys.path，按 P4 隔离法）。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Redis off for the whole module — every cfg_get/cfg_set degrades to default,
# so these tests exercise the pure-logic / graceful-degradation paths only.
os.environ.setdefault("TM_REDIS_ENABLED", "0")

import config_store  # noqa: E402
import dreaming  # noqa: E402
import governance_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch, tmp_path):
    """Fresh WORKSPACE + reset process singletons per test so config/governance
    DBs are isolated and no override bleeds across cases."""
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    monkeypatch.setenv("TM_REDIS_ENABLED", "0")
    config_store.reset_for_tests()
    dreaming.reset_for_tests()
    yield
    config_store.reset_for_tests()
    dreaming.reset_for_tests()


def _run(coro):
    return asyncio.run(coro)


# ── resolve_container_dream_config: global / container override + inherit ─────


def test_resolve_inherits_global_when_container_unset(monkeypatch):
    # No container override → inherit global trigger_cron + batch_model.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:trigger_cron": "0 5 * * *",
        "config:dreaming:batch_model": "global-model",
    }))
    # container-level cfg_get all miss → None → inherit.
    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _async_none)
    cfg = _run(dreaming.resolve_container_dream_config("alpha"))
    assert cfg == {"enabled": True, "cron": "0 5 * * *", "model": "global-model"}


def test_resolve_container_overrides_global(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:trigger_cron": "0 5 * * *",
        "config:dreaming:batch_model": "global-model",
    }))

    async def _cfg_get(key, default=None):
        table = {
            "config:dreaming:container:alpha:enabled": "true",
            "config:dreaming:container:alpha:cron": "30 1 * * *",
            "config:dreaming:container:alpha:model": "alpha-model",
        }
        return table.get(key, None)

    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _cfg_get)
    cfg = _run(dreaming.resolve_container_dream_config("alpha"))
    assert cfg["cron"] == "30 1 * * *"
    assert cfg["model"] == "alpha-model"
    assert cfg["enabled"] is True


def test_resolve_container_disabled_overrides(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
    }))

    async def _cfg_get(key, default=None):
        return "false" if key.endswith(":enabled") else None

    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _cfg_get)
    cfg = _run(dreaming.resolve_container_dream_config("beta"))
    assert cfg["enabled"] is False


def test_resolve_global_disabled_forces_container_disabled(monkeypatch):
    # global_enabled false → container reported disabled regardless of its flag.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": False,
    }))
    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _async_true_enabled)
    cfg = _run(dreaming.resolve_container_dream_config("gamma"))
    assert cfg == {"enabled": False, "cron": None, "model": None}


# ── run_dream_cycle: global gate + report-only ───────────────────────────────


def test_cycle_skipped_when_global_disabled(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": False,
    }))
    rep = _run(dreaming.run_dream_cycle(container="x", dry_run=True))
    assert rep["status"] == "skipped_global_disabled"
    assert rep["actions"] == []
    assert rep["excluded_from_rag"] is True


def test_cycle_dry_run_is_report_only(monkeypatch):
    # One enabled container; dry_run True → every destructive action applied=False.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:trigger_cron": "0 2 * * *",
        "config:dreaming:batch_model": "",
        "config:dreaming:graph_prune_enabled": True,
        "config:dreaming:prune_apply": False,
    }))
    monkeypatch.setattr(dreaming, "_list_enabled_containers", lambda scope: ["alpha"])
    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _async_true_enabled)
    rep = _run(dreaming.run_dream_cycle(container="alpha", dry_run=True))
    assert rep["status"] == "ok"
    assert rep["actions"], "expected at least one action for an enabled container"
    assert all(a["applied"] is False for a in rep["actions"])
    # The destructive prune candidates must be present and report-only.
    prune = [a for a in rep["actions"] if a["tool"].startswith("prune_")]
    assert prune and all(a["applied"] is False for a in prune)


def test_cycle_prune_apply_off_blocks_delete_even_when_not_dry_run(monkeypatch):
    # dry_run False but prune_apply config false → still report-only (the double
    # guard). No action may be applied.
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:graph_prune_enabled": True,
        "config:dreaming:prune_apply": False,
    }))
    monkeypatch.setattr(dreaming, "_list_enabled_containers", lambda scope: ["alpha"])
    monkeypatch.setattr(dreaming.redis_client, "cfg_get", _async_true_enabled)
    rep = _run(dreaming.run_dream_cycle(container="alpha", dry_run=False))
    assert rep["status"] == "ok"
    assert all(a["applied"] is False for a in rep["actions"])
    assert "report-only" in rep["notes"]


def test_cycle_persists_to_governance_store(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:global_enabled": True,
        "config:dreaming:prune_apply": False,
    }))
    monkeypatch.setattr(dreaming, "_list_enabled_containers", lambda scope: [])
    rep = _run(dreaming.run_dream_cycle(container=None, dry_run=True))
    assert rep["status"] == "ok"
    last = governance_store.get_last_report()
    assert last is not None
    assert last["exclude_from_rag"] is True
    assert last["system_type"] == "governance"


# ── governance_store round-trip + immunity metadata ──────────────────────────


def test_governance_store_roundtrip_stamps_immunity():
    assert governance_store.write_dream_report({"status": "ok", "container_scope": "all"})
    last = governance_store.get_last_report()
    assert last is not None
    assert last["system_type"] == "governance"
    assert last["exclude_from_rag"] is True
    reports = governance_store.list_reports(limit=10)
    assert len(reports) == 1
    assert reports[0]["exclude_from_rag"] is True


def test_governance_store_immunity_meta_not_overridable():
    # A caller trying to sneak exclude_from_rag=False is force-corrected.
    governance_store.write_dream_report(
        {"status": "ok", "exclude_from_rag": False, "system_type": "user"}
    )
    last = governance_store.get_last_report()
    assert last["exclude_from_rag"] is True
    assert last["system_type"] == "governance"


def test_governance_store_list_newest_first():
    governance_store.write_dream_report({"status": "ok", "seq": 1})
    governance_store.write_dream_report({"status": "ok", "seq": 2})
    reports = governance_store.list_reports(limit=10)
    assert [r.get("seq") for r in reports] == [2, 1]


# ── scheduler: stays off when scheduler_enabled=false (behavior preserving) ───


def test_scheduler_not_started_when_disabled(monkeypatch):
    monkeypatch.setattr(config_store, "get_cached", _fake_cfg({
        "config:dreaming:scheduler_enabled": False,
    }))
    started = _run(dreaming.start_scheduler())
    assert started is False
    assert dreaming._scheduler_running() is False


def test_scheduler_default_is_disabled_no_override(monkeypatch):
    # No config override at all → get_cached returns the caller default (False)
    # for scheduler_enabled → scheduler must NOT start (the deploy-time behavior-
    # preserving guarantee).
    started = _run(dreaming.start_scheduler())
    assert started is False
    assert dreaming._scheduler_running() is False


# ── helpers ──────────────────────────────────────────────────────────────────


def _fake_cfg(table):
    def _get(key, default=None):
        return table.get(key, default)

    return _get


async def _async_none(key, default=None):
    return None


async def _async_true_enabled(key, default=None):
    # container :enabled → true; everything else (cron/model) → miss (inherit).
    return "true" if key.endswith(":enabled") else None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
