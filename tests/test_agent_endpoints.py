"""治理 agent 后端调度 endpoint 单测（task_rag_server，Phase 3）。

经 FastAPI TestClient 打 /admin/agent/* 端点，全 mock（不连真 LLM / worker：
conftest 默认 TM_DISABLE_WORKER=1，故 wait=False 入队即返回 job_id 不跑子进程）。
覆盖：

  * 总闸 TM_AGENT_ORCHESTRATION_ENABLED：缺省/0 → status='disabled' 不入队；
    1 → 入队返回 run_id + job_id，并落 agent_runs 头。
  * GET /admin/agent/runs / approvals：读隔离 governance store。
  * approve 是【唯一】对破坏性工具调 invoke_tool(dry_run=False) 真执行的路径；
    reject 不执行；过期/不存在 → 404。
  * 鉴权：缺 key → 401。

隔离：conftest.load_server 重载 server module 注入 fresh WORKSPACE + RAG_API_KEY；
endpoint 总闸经 extra_env 传入（env 在 module import 时被 _env_bool 读）。
"""
from __future__ import annotations

from pathlib import Path

from conftest import API_KEY, auth_headers, load_server, make_workspace
from fastapi.testclient import TestClient


def _setup(tmp_path: Path, monkeypatch, enabled: bool):
    """加载 server（总闸按需开/关），返回 (server_module, TestClient)。

    把 admit-gate 的负载阈值抬到极高，避免开发/CI 机器系统负载偏高时入队被
    503（本套件只验调度契约，不验背压门）。"""
    workspace = make_workspace(tmp_path)
    extra = {
        "TM_AGENT_ORCHESTRATION_ENABLED": "1" if enabled else "0",
        "TM_MAX_LOAD_PER_CPU": "100000",
    }
    server = load_server(workspace, monkeypatch, extra)
    return server, TestClient(server.app)


# ── 总闸 OFF → disabled，不入队 ───────────────────────────────────────────────


def test_invoke_disabled_when_flag_off(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=False)
    resp = client.post(
        "/admin/agent/dream-orchestrator/invoke",
        json={"container": "box", "goal": "tidy up", "dry_run": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "disabled"
    assert data["run_id"] == ""
    assert data["agent_name"] == "dream-orchestrator"
    # 关闸不应落任何 run。
    assert server.governance_store.list_agent_runs() == []


# ── 总闸 ON → 入队返回 run_id/job_id，落 run 头 ───────────────────────────────


def test_invoke_enqueues_when_flag_on(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    resp = client.post(
        "/admin/agent/dream-orchestrator/invoke",
        json={"container": "box", "goal": "compress clusters", "dry_run": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "enqueued"
    assert data["run_id"].startswith("agentrun-")
    assert isinstance(data["job_id"], int)
    assert data["container"] == "box"
    # run 头已落隔离 governance store（status='enqueued'）。
    runs = server.governance_store.list_agent_runs()
    assert any(r["run_id"] == data["run_id"] and r["status"] == "enqueued"
               for r in runs)
    # 入队后 GET /admin/agent/runs 能读到该 run。
    listed = client.get("/admin/agent/runs", headers=auth_headers())
    assert listed.status_code == 200
    assert any(r["run_id"] == data["run_id"] for r in listed.json()["runs"])


def test_invoke_apply_requires_both_dry_run_false_and_allow_apply(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    # allow_apply=True 但 dry_run 仍 True（默认）→ effective apply 仍 False。
    resp = client.post(
        "/admin/agent/a/invoke",
        json={"container": "box", "allow_apply": True, "dry_run": True},
        headers=auth_headers(),
    )
    assert resp.json()["allow_apply"] is False
    # dry_run=false 且 allow_apply=true → effective apply True。
    resp2 = client.post(
        "/admin/agent/a/invoke",
        json={"container": "box", "allow_apply": True, "dry_run": False},
        headers=auth_headers(),
    )
    assert resp2.json()["allow_apply"] is True


def test_invoke_global_run_without_container(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    resp = client.post(
        "/admin/agent/a/invoke",
        json={"goal": "global sweep", "dry_run": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "enqueued"
    assert data["container"] is None  # 全局 run scope_container 为 None


# ── 鉴权 ──────────────────────────────────────────────────────────────────────


def test_invoke_requires_auth(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    resp = client.post("/admin/agent/a/invoke", json={"container": "box"})
    assert resp.status_code == 401


def test_runs_and_approvals_require_auth(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    assert client.get("/admin/agent/runs").status_code == 401
    assert client.get("/admin/agent/approvals").status_code == 401


# ── approve 是唯一触发破坏性真执行的路径 ─────────────────────────────────────


def test_approve_is_the_only_destructive_real_execution_path(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)

    # 预置一条 pending 破坏性审批（模拟循环内被 park 的请求）。
    approval_id = server.governance_store.write_agent_approval(
        run_id="r1", agent_name="a", container="box",
        tool="snapshot_and_quarantine", params_json='{"max_age_days": 90}',
    )
    assert isinstance(approval_id, int)

    # spy invoke_tool：记录是否以 dry_run=False 真执行破坏性工具。
    invoked: list = []

    async def _spy_invoke(tool, container=None, params=None, dry_run=True):
        invoked.append((tool, container, dict(params or {}), dry_run))
        return {"tool": tool, "status": "applied", "container": container,
                "result": {"reindex_required": False}, "applied": True,
                "notes": "executed"}

    monkeypatch.setattr(server.governance_tools, "invoke_tool", _spy_invoke)

    # GET approvals 先确认 pending 可见。
    listed = client.get("/admin/agent/approvals?status=pending", headers=auth_headers())
    assert listed.status_code == 200
    assert any(a["id"] == approval_id for a in listed.json()["approvals"])

    # approve → 唯一对破坏性工具 invoke_tool(dry_run=False)。
    resp = client.post(
        f"/admin/agent/approvals/{approval_id}/approve", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["tool"] == "snapshot_and_quarantine"
    assert body["applied"] is True
    # 关键不变量：恰以 dry_run=False、用记录的 params 执行了一次。
    assert invoked == [
        ("snapshot_and_quarantine", "box", {"max_age_days": 90}, False)
    ]


def test_reject_does_not_execute(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    approval_id = server.governance_store.write_agent_approval(
        run_id="r1", agent_name="a", container="box",
        tool="snapshot_and_quarantine", params_json="{}",
    )
    invoked: list = []

    async def _spy_invoke(tool, container=None, params=None, dry_run=True):
        invoked.append((tool, dry_run))
        return {"tool": tool, "status": "applied", "container": container,
                "result": {}, "applied": True, "notes": ""}

    monkeypatch.setattr(server.governance_tools, "invoke_tool", _spy_invoke)
    resp = client.post(
        f"/admin/agent/approvals/{approval_id}/reject", headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert invoked == []  # reject 绝不执行任何工具


def test_approve_unknown_id_returns_404(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    resp = client.post("/admin/agent/approvals/999999/approve", headers=auth_headers())
    assert resp.status_code == 404


def test_approve_already_decided_returns_404(tmp_path, monkeypatch):
    server, client = _setup(tmp_path, monkeypatch, enabled=True)
    approval_id = server.governance_store.write_agent_approval(
        run_id="r1", agent_name="a", container="box",
        tool="snapshot_and_quarantine", params_json="{}",
    )

    async def _spy_invoke(tool, container=None, params=None, dry_run=True):
        return {"tool": tool, "status": "applied", "container": container,
                "result": {}, "applied": True, "notes": ""}

    monkeypatch.setattr(server.governance_tools, "invoke_tool", _spy_invoke)
    first = client.post(
        f"/admin/agent/approvals/{approval_id}/approve", headers=auth_headers())
    assert first.status_code == 200
    # 二次 approve 同一条 → 已 decided → 404（防重复执行破坏性动作）。
    second = client.post(
        f"/admin/agent/approvals/{approval_id}/approve", headers=auth_headers())
    assert second.status_code == 404


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
