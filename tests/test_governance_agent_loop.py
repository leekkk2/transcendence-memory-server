"""治理编排 agent 循环 + 安全闸单测（governance_agent）。

全 mock 网关（rag_engine.llm_chat_with_tools），不连真 LLM / lancedb / fastapi。
覆盖：

  * decide() 安全闸决策表逐档：SAFE 只读恒真执行 / 可逆 allow_apply 落地 /
    可逆默认 dry-run / 破坏性恒 blocked 进审批。
  * run_agent 循环：scripted tool_calls → 走 invoke_tool、结果被 _truncate_for_llm
    截断回灌、finish 收尾、停止条件（completed / max_steps / 破坏性 pending_approval /
    stalled）命中。
  * 破坏性工具：模型请求 apply → 记 pending approval（write_agent_approval），
    绝不调 invoke_tool(dry_run=False) 真执行。

与既有 governance 套件同款隔离法：scripts/ 注入 sys.path、TM_REDIS_ENABLED=0、
独立 WORKSPACE + config_store 单例重置。
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
import governance_agent  # noqa: E402
import governance_store  # noqa: E402
import governance_tools  # noqa: E402
import rag_engine  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_workspace(monkeypatch, tmp_path):
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


def _cfg(**kwargs) -> governance_agent.AgentConfig:
    base = dict(max_steps=6, token_budget=60000, per_tool_result_bytes=8000)
    base.update(kwargs)
    return governance_agent.AgentConfig(**base)


# ── tool_call message builder (OpenAI wire shape the loop parses) ─────────────


def _msg_with_calls(*calls: tuple[str, dict], content: str = "") -> dict:
    tool_calls = []
    for i, (name, args) in enumerate(calls):
        tool_calls.append({
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def _msg_no_calls(content: str = "done") -> dict:
    return {"role": "assistant", "content": content}


class _ScriptedGateway:
    """A gateway stub that returns a pre-scripted list of assistant messages, one
    per loop step; records the tools array it was handed."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.calls = 0
        self.seen_tools: list[list[dict]] | None = None

    async def __call__(self, messages, tools=None, tool_choice="auto"):
        if self.seen_tools is None:
            self.seen_tools = tools
        i = min(self.calls, len(self._messages) - 1)
        self.calls += 1
        return self._messages[i]


def _patch_gateway(monkeypatch, messages: list[dict]) -> _ScriptedGateway:
    gw = _ScriptedGateway(messages)
    monkeypatch.setattr(rag_engine, "llm_chat_with_tools", gw)
    return gw


def _write_cluster(tmp_path: Path, container: str) -> None:
    now = int(time.time())
    rows = [
        {"id": "a1", "title": "t1", "text": "python rag note one with enough chars",
         "tags": ["python", "rag"], "updatedAt": now},
        {"id": "a2", "title": "t2", "text": "python rag note two with enough chars",
         "tags": ["python", "rag"], "updatedAt": now},
    ]
    root = tmp_path / "tasks" / "rag" / "containers" / container
    root.mkdir(parents=True, exist_ok=True)
    with (root / "memory_objects.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# ── decide() 决策表逐档 ───────────────────────────────────────────────────────


def test_decide_safe_readonly_always_runs_for_real():
    cfg = _cfg(allow_apply=False)
    for tool in ("manage_token_quotas", "analyze_retrieval_latency"):
        gate = governance_agent.decide(tool, {}, "box", cfg)
        assert gate["blocked"] is False
        assert gate["effective_dry_run"] is False  # dry_run 对只读是 no-op，真读


def test_decide_reversible_dry_run_without_apply_authority():
    cfg = _cfg(allow_apply=False)
    for tool in ("compress_knowledge_cluster", "update_container_routing",
                 "tune_model_parameters"):
        gate = governance_agent.decide(tool, {}, "box", cfg)
        assert gate["blocked"] is False
        assert gate["effective_dry_run"] is True  # 无 apply 授权 → 预览


def test_decide_reversible_applies_with_apply_authority():
    cfg = _cfg(allow_apply=True)
    for tool in ("compress_knowledge_cluster", "update_container_routing",
                 "tune_model_parameters"):
        gate = governance_agent.decide(tool, {}, "box", cfg)
        assert gate["blocked"] is False
        assert gate["effective_dry_run"] is False  # apply 授权 → 真落地


def test_decide_destructive_always_blocked_even_with_apply():
    # 破坏性工具在 allow_apply=True 时仍恒 blocked → 进审批，绝不循环内自动执行。
    for allow in (False, True):
        gate = governance_agent.decide(
            "snapshot_and_quarantine", {}, "box", _cfg(allow_apply=allow))
        assert gate["blocked"] is True
        assert gate["requires"] == "approval"
        assert gate["effective_dry_run"] is True  # belt-and-suspenders


def test_decide_unknown_tool_is_permissive_not_blocked():
    gate = governance_agent.decide("no_such_tool", {}, "box", _cfg())
    assert gate["blocked"] is False  # invoke_tool 会回 status='error'，gate 放行让其报错


# ── run_agent 循环：finish 收尾 + 工具执行 + 结果截断 ─────────────────────────


def test_loop_runs_readonly_tool_then_finish_completed(monkeypatch, tmp_path):
    gw = _patch_gateway(monkeypatch, [
        _msg_with_calls(("manage_token_quotas", {})),
        _msg_with_calls(("finish", {"summary": "checked quotas, all nominal"})),
    ])
    out = _run(governance_agent.run_agent(
        goal="inspect quotas", container=None,
        agent_name="dream-orchestrator", run_id="r-readonly",
        cfg=_cfg(),
    ))
    assert out["status"] == "completed"
    assert out["final_summary"] == "checked quotas, all nominal"
    assert out["run_id"] == "r-readonly"
    assert out["approvals"] == []
    # tools 数组确实被传给网关（build_tool_specs 派生）。
    names = {t["function"]["name"] for t in (gw.seen_tools or [])}
    assert "manage_token_quotas" in names
    assert "finish" in names


def test_loop_no_tool_call_completes(monkeypatch):
    _patch_gateway(monkeypatch, [_msg_no_calls("nothing to do here")])
    out = _run(governance_agent.run_agent(
        goal="x", container=None, agent_name="a", run_id="r-empty", cfg=_cfg(),
    ))
    assert out["status"] == "completed"
    assert "nothing to do" in out["final_summary"]


def test_loop_truncates_oversized_tool_result(monkeypatch, tmp_path):
    # invoke_tool 回一个超大 result → 回灌 messages 的 tool 内容被 clamp 到
    # per_tool_result_bytes（防循环内重演 10 MiB 400）。
    captured: dict = {}

    async def _fake_invoke(name, container, args, dry_run=True):
        return {"tool": name, "status": "ok", "container": container,
                "result": {"blob": "Z" * 50_000}, "applied": False, "notes": ""}

    monkeypatch.setattr(governance_tools, "invoke_tool", _fake_invoke)

    real_truncate = governance_tools._truncate_for_llm

    def _spy_truncate(text, budget):
        captured["budget"] = budget
        captured["in_len"] = len(text.encode("utf-8"))
        out = real_truncate(text, budget)
        captured["out_len"] = len(out.encode("utf-8"))
        return out

    monkeypatch.setattr(governance_tools, "_truncate_for_llm", _spy_truncate)
    _patch_gateway(monkeypatch, [
        _msg_with_calls(("analyze_retrieval_latency", {})),
        _msg_with_calls(("finish", {"summary": "done"})),
    ])
    out = _run(governance_agent.run_agent(
        goal="x", container="box", agent_name="a", run_id="r-trunc",
        cfg=_cfg(per_tool_result_bytes=500),
    ))
    assert out["status"] == "completed"
    assert captured["budget"] == 500
    assert captured["in_len"] > 500  # 入是巨 blob
    assert captured["out_len"] <= 500  # 出被夹到预算内


def test_loop_max_steps_exhausted(monkeypatch):
    # 模型每步调一个只读工具、永不 finish、每步结果各不相同（避开 stall 检测）→
    # 撞 max_steps 出局（不抛）。fake invoke 让结果随 step 变化保证签名不重复。
    step_counter = {"n": 0}

    async def _varying_invoke(name, container, args, dry_run=True):
        step_counter["n"] += 1
        return {"tool": name, "status": "ok", "container": container,
                "result": {"step": step_counter["n"]}, "applied": False, "notes": ""}

    monkeypatch.setattr(governance_tools, "invoke_tool", _varying_invoke)
    _patch_gateway(monkeypatch, [_msg_with_calls(("analyze_retrieval_latency", {}))])
    out = _run(governance_agent.run_agent(
        goal="loop forever", container="box", agent_name="a", run_id="r-max",
        cfg=_cfg(max_steps=3),
    ))
    assert out["status"] == "max_steps_exhausted"
    assert out["steps"] == 3


def test_loop_stalls_on_identical_repeat(monkeypatch):
    # 连续两步 (tool,args)→相同结果 → stalled（loop 检测）。每步单独一个 tool_call，
    # 这样签名比对落在跨步的 last_signature 上。
    async def _fixed_invoke(name, container, args, dry_run=True):
        return {"tool": name, "status": "ok", "container": container,
                "result": {"same": 1}, "applied": False, "notes": ""}

    monkeypatch.setattr(governance_tools, "invoke_tool", _fixed_invoke)
    _patch_gateway(monkeypatch, [
        _msg_with_calls(("analyze_retrieval_latency", {"window": "7d"})),
        _msg_with_calls(("analyze_retrieval_latency", {"window": "7d"})),
        _msg_with_calls(("finish", {"summary": "unreached"})),
    ])
    out = _run(governance_agent.run_agent(
        goal="x", container="box", agent_name="a", run_id="r-stall", cfg=_cfg(),
    ))
    assert out["status"] == "stalled"


# ── 破坏性工具：进审批不执行（核心安全不变量） ───────────────────────────────


def test_destructive_request_records_approval_never_executes(monkeypatch):
    # 模型请求 snapshot_and_quarantine apply → 记 pending approval，
    # invoke_tool 绝不被以 dry_run=False 调用执行破坏性动作。
    invoked: list = []

    async def _tracking_invoke(name, container, args, dry_run=True):
        invoked.append((name, dry_run))
        return {"tool": name, "status": "ok", "container": container,
                "result": {}, "applied": False, "notes": ""}

    monkeypatch.setattr(governance_tools, "invoke_tool", _tracking_invoke)
    _patch_gateway(monkeypatch, [
        _msg_with_calls(("snapshot_and_quarantine", {"max_age_days": 90})),
    ])
    out = _run(governance_agent.run_agent(
        goal="quarantine stale", container="box", agent_name="a",
        run_id="r-destructive", cfg=_cfg(allow_apply=True),  # 即便授权 apply
    ))
    assert out["status"] == "blocked_pending_approval"
    assert len(out["approvals"]) == 1
    # 破坏性工具从未经 invoke_tool 执行（连 dry_run 都没走真执行 handler）。
    assert ("snapshot_and_quarantine", False) not in invoked
    assert invoked == []  # 整步只走了审批分支，没碰 invoke_tool
    # 审批行确已落库（pending），是循环外人工 actuator 的唯一入口。
    pendings = governance_store.list_agent_approvals(status="pending")
    assert any(p["tool"] == "snapshot_and_quarantine" for p in pendings)
    assert any(p["run_id"] == "r-destructive" for p in pendings)


def test_reversible_apply_executes_real_with_authority(monkeypatch, tmp_path):
    # 可逆 compress + allow_apply=True → invoke_tool 以 dry_run=False 真执行
    # （走真 handler，LLM 经 _llm_oneshot mock）。验证授权落地路径。
    _write_cluster(tmp_path, "box")

    async def _fake_card(prompt, system_prompt=None):
        return "假索引卡：同义词头部 / 核心结论 / 来源 id"

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _fake_card)
    _patch_gateway(monkeypatch, [
        _msg_with_calls(("compress_knowledge_cluster", {})),
        _msg_with_calls(("finish", {"summary": "compressed"})),
    ])
    out = _run(governance_agent.run_agent(
        goal="compress", container="box", agent_name="a", run_id="r-apply",
        cfg=_cfg(allow_apply=True),
    ))
    assert out["status"] == "completed"
    # compress 真执行追加了索引卡 → reindex_required → run 记录该容器待重建。
    assert out["reindex_containers"] == ["box"]
    rows = [json.loads(l) for l in (
        tmp_path / "tasks" / "rag" / "containers" / "box" / "memory_objects.jsonl"
    ).read_text(encoding="utf-8").splitlines() if l]
    assert any(r["id"].startswith("idxcard-") for r in rows)  # 卡已落盘


def test_reversible_dry_run_does_not_apply_without_authority(monkeypatch, tmp_path):
    # 同 compress 但 allow_apply=False → 仅预览，源文件零改动、无新卡。
    _write_cluster(tmp_path, "box")

    async def _must_not_call(prompt, system_prompt=None):
        raise AssertionError("dry_run compress must not call the LLM")

    monkeypatch.setattr(governance_tools, "_llm_oneshot", _must_not_call)
    _patch_gateway(monkeypatch, [
        _msg_with_calls(("compress_knowledge_cluster", {})),
        _msg_with_calls(("finish", {"summary": "previewed"})),
    ])
    out = _run(governance_agent.run_agent(
        goal="compress", container="box", agent_name="a", run_id="r-dry",
        cfg=_cfg(allow_apply=False),
    ))
    assert out["status"] == "completed"
    assert out["reindex_containers"] == []  # 预览不触发重建
    rows = [json.loads(l) for l in (
        tmp_path / "tasks" / "rag" / "containers" / "box" / "memory_objects.jsonl"
    ).read_text(encoding="utf-8").splitlines() if l]
    assert not any(r["id"].startswith("idxcard-") for r in rows)  # 无新卡


# ── 降级：网关故障不抛进 caller ───────────────────────────────────────────────


def test_gateway_outage_degrades_not_raise(monkeypatch):
    async def _boom(messages, tools=None, tool_choice="auto"):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(rag_engine, "llm_chat_with_tools", _boom)
    out = _run(governance_agent.run_agent(
        goal="x", container=None, agent_name="a", run_id="r-down", cfg=_cfg(),
    ))
    assert out["status"] == "gateway_error"
    assert "unavailable" in out["final_summary"]


# ── build_tool_specs：派生工具 schema ─────────────────────────────────────────


def test_build_tool_specs_derives_from_registry_plus_finish():
    specs = governance_agent.build_tool_specs(include_dream=False)
    names = {s["function"]["name"] for s in specs}
    # 注册表里的治理工具都在。
    assert {"compress_knowledge_cluster", "snapshot_and_quarantine",
            "manage_token_quotas"} <= names
    assert "finish" in names  # 合成收尾工具
    assert "run_dream_scan" not in names  # include_dream=False 时不带
    # 每个 spec 是合法 OpenAI function 形状。
    for s in specs:
        assert s["type"] == "function"
        assert isinstance(s["function"]["parameters"], dict)


def test_build_tool_specs_destructive_tag_in_description():
    specs = governance_agent.build_tool_specs(include_dream=False)
    by_name = {s["function"]["name"]: s for s in specs}
    snap = by_name["snapshot_and_quarantine"]["function"]["description"]
    assert "destructive=true" in snap  # 安全姿态 meta 注入 description


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
