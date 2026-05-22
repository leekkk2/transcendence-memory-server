"""Tests for async knowledge-graph document ingestion (v0.15.0).

`POST /documents/text` and `/documents/upload` used to block the request thread
on the full LightRAG / RAG-Anything graph build (tens of seconds to minutes),
tripping the 100s edge-proxy timeout. They are now fully asynchronous: the
handler stages input under the container `_inbox` and enqueues a job; the
background worker drives `task_rag_graph_ingest.py` as a child process.

Covered here:
- `default_command_resolver` maps the two new ops to the graph-ingest CLI.
- `_build_ingest_cmd` (server-side mirror of the resolver) does the same.
- `/documents/text` returns a `CommandResponse` (job id in `pid`), not the old
  `QueryResponse`, and concurrent posts are NOT coalesced (distinct jobs).
- the worker injects `CONTAINER` env for the new ops' child process.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import API_KEY, auth_headers, load_server, make_workspace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def worker_module():
    for name in ("scripts.job_worker", "scripts.job_queue", "job_worker", "job_queue"):
        sys.modules.pop(name, None)
    return importlib.import_module("scripts.job_worker")


@pytest.fixture
def queue_module():
    sys.modules.pop("scripts.job_queue", None)
    sys.modules.pop("job_queue", None)
    return importlib.import_module("scripts.job_queue")


def _make_job(queue_module, op: str, container: str, payload: dict):
    return queue_module.Job(
        id=1, op=op, container=container, payload=payload,
        status="running", attempts=1, max_attempts=5,
        enqueued_at=0, next_run_at=0,
        started_at=None, finished_at=None, result_code=None,
        last_error=None, pid=None, label="",
    )


# ---------- command resolution ----------


def test_resolver_maps_ingest_document_text(queue_module, worker_module, tmp_path):
    resolver = worker_module.default_command_resolver(tmp_path / "scripts")
    job = _make_job(queue_module, "ingest-document-text", "alpha",
                    {"input_path": "/inbox/text-abc.txt", "description": "note"})
    cmd = resolver(job)
    assert "task_rag_graph_ingest.py" in cmd[0]
    assert cmd[cmd.index("--mode") + 1] == "text"
    assert cmd[cmd.index("--input") + 1] == "/inbox/text-abc.txt"
    assert cmd[cmd.index("--container") + 1] == "alpha"
    assert cmd[cmd.index("--description") + 1] == "note"


def test_resolver_maps_ingest_document_file(queue_module, worker_module, tmp_path):
    resolver = worker_module.default_command_resolver(tmp_path / "scripts")
    job = _make_job(queue_module, "ingest-document-file", "beta",
                    {"input_path": "/inbox/file-xyz.pdf", "parse_method": "ocr"})
    cmd = resolver(job)
    assert "task_rag_graph_ingest.py" in cmd[0]
    assert cmd[cmd.index("--mode") + 1] == "file"
    assert cmd[cmd.index("--input") + 1] == "/inbox/file-xyz.pdf"
    assert cmd[cmd.index("--parse-method") + 1] == "ocr"


def test_resolver_rejects_unknown_op(queue_module, worker_module, tmp_path):
    resolver = worker_module.default_command_resolver(tmp_path / "scripts")
    with pytest.raises(ValueError):
        resolver(_make_job(queue_module, "weird-op", "gamma", {}))


def test_build_ingest_cmd_mirrors_resolver(tmp_path, monkeypatch):
    """task_rag_server._build_ingest_cmd must map the new ops the same way as
    the worker resolver, so the wait=True inline path stays consistent."""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    cmd_text = server._build_ingest_cmd(
        "ingest-document-text", "alpha", {"input_path": "/inbox/t.txt"})
    assert "task_rag_graph_ingest.py" in cmd_text[0]
    assert cmd_text[cmd_text.index("--mode") + 1] == "text"
    cmd_file = server._build_ingest_cmd(
        "ingest-document-file", "beta", {"input_path": "/inbox/f.pdf"})
    assert cmd_file[cmd_file.index("--mode") + 1] == "file"


# ---------- child-process env passthrough ----------


def test_worker_injects_container_env_for_graph_ingest(queue_module, worker_module, tmp_path):
    """The default env_resolver must inject job.container into CONTAINER env so
    the graph-ingest child resolves the per-container embedding/LLM route."""
    job = _make_job(queue_module, "ingest-document-text", "myapp_openai",
                    {"input_path": "/inbox/t.txt"})
    worker = worker_module.JobWorker(
        queue=queue_module.JobQueue(tmp_path / "q.db"),
        command_resolver=lambda _j: ["true"],
    )
    env = worker.env_resolver(job)
    assert env["CONTAINER"] == "myapp_openai"


# ---------- HTTP contract ----------


def _load_client(tmp_path, monkeypatch):
    """Server + TestClient with the LightRAG readiness gate stubbed out, so the
    endpoint test does not depend on the optional lightrag package."""
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    monkeypatch.setattr(server, "_require_lightrag_ready", lambda: None)
    return server, TestClient(server.app)


def test_documents_text_returns_command_response_not_query_response(tmp_path, monkeypatch):
    server, client = _load_client(tmp_path, monkeypatch)
    resp = client.post(
        "/documents/text",
        headers=auth_headers(),
        json={"container": "testbox", "text": "async graph ingestion " * 40},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # CommandResponse shape: job id surfaced in `pid`, status reflects enqueue.
    assert body.get("pid") is not None
    assert body.get("status") == "enqueued"
    assert body.get("background") is True
    # Old QueryResponse fields must be gone.
    assert "answer" not in body and "mode" not in body


def test_documents_text_concurrent_posts_are_not_coalesced(tmp_path, monkeypatch):
    """Each /documents/text carries a distinct staged input file; coalescing
    would silently drop the 2nd document. Posts must yield distinct job ids."""
    server, client = _load_client(tmp_path, monkeypatch)
    pids = set()
    for i in range(3):
        resp = client.post(
            "/documents/text",
            headers=auth_headers(),
            json={"container": "testbox", "text": f"doc number {i} body text"},
        )
        assert resp.status_code == 200, resp.text
        pids.add(resp.json()["pid"])
    assert len(pids) == 3, f"expected 3 distinct jobs, got {pids}"


def test_documents_text_stages_input_under_inbox(tmp_path, monkeypatch):
    server, client = _load_client(tmp_path, monkeypatch)
    resp = client.post(
        "/documents/text",
        headers=auth_headers(),
        json={"container": "testbox", "text": "inbox staging check"},
    )
    assert resp.status_code == 200, resp.text
    inbox = server.WS / "tasks" / "rag" / "containers" / "testbox" / "_inbox"
    staged = list(inbox.glob("text-*.txt"))
    assert len(staged) == 1
    assert staged[0].read_text(encoding="utf-8") == "inbox staging check"


def test_documents_upload_route_registered_as_command_response(tmp_path, monkeypatch):
    """The new /documents/upload route exists and is an alias of /documents/file
    (both async, both typed as CommandResponse)."""
    server, _client = _load_client(tmp_path, monkeypatch)
    routes = {r.path: r for r in server.app.routes if getattr(r, "path", None)}
    assert "/documents/upload" in routes
    assert "/documents/file" in routes
    assert routes["/documents/upload"].response_model is server.CommandResponse
    assert routes["/documents/file"].response_model is server.CommandResponse
    assert routes["/documents/text"].response_model is server.CommandResponse
