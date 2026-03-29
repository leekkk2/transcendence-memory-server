import contextlib
import importlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient


API_KEY = "test-rag-key"
CONTAINER = "proof"
TASK_ID = "TASK-20260329-004"
TASK_SUMMARY = "任务卡基线内容。"
PROOF_TEXT = "月海罗盘对象证据，可用于 feat-004 检索证明。"
QUERY_TEXT = "月海罗盘对象证据"


def load_server_module(
    workspace: Path,
    monkeypatch,
    extra_env: dict[str, str] | None = None,
):
    monkeypatch.setenv("WORKSPACE", str(workspace))
    monkeypatch.setenv("RAG_API_KEY", API_KEY)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)

    module_name = "scripts.task_rag_server"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def create_proof_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "scripts").symlink_to(Path(__file__).resolve().parents[1] / "scripts")

    task_card = workspace / "tasks" / "active" / f"{TASK_ID}-feat004-proof.md"
    task_card.parent.mkdir(parents=True)
    task_card.write_text(
        "\n".join(
            [
                "## Meta",
                "- Project: transcendence-memory",
                "- Status: active",
                "- Tags: feat-004, proof",
                "",
                "## Summary",
                TASK_SUMMARY,
            ]
        ),
        encoding="utf-8",
    )
    return workspace


def post_ok(client: TestClient, path: str, payload: dict[str, object]) -> dict[str, object]:
    response = client.post(path, headers={"X-API-KEY": API_KEY}, json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0, body
    return body


def embedding_vector(text: str) -> list[float]:
    if "月海罗盘" in text:
        return [1.0, 0.0, 0.0]
    if TASK_SUMMARY in text:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


@contextlib.contextmanager
def fake_embedding_server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            response = {
                "data": [{"embedding": embedding_vector(payload["input"])}]
            }
            raw = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_ingest_memory_objects_are_merged_into_manifest(tmp_path: Path, monkeypatch):
    workspace = create_proof_workspace(tmp_path)
    server = load_server_module(workspace, monkeypatch)
    client = TestClient(server.app)

    ingest_response = client.post(
        "/ingest-memory/objects",
        headers={"X-API-KEY": API_KEY},
        json={
            "container": CONTAINER,
            "objects": [
                {
                    "id": TASK_ID,
                    "text": PROOF_TEXT,
                    "title": "feat-004 typed ingest proof",
                    "source": "pytest",
                    "tags": ["feat-004", "typed-objects"],
                    "metadata": {"taskId": TASK_ID, "kind": "memory_object"},
                }
            ],
        },
    )
    assert ingest_response.status_code == 200
    ingest_body = ingest_response.json()
    assert ingest_body["accepted"] == 1
    assert ingest_body["stored_paths"]

    post_ok(client, "/build-manifest", {"container": CONTAINER})

    manifest_path = workspace / "tasks" / "rag" / "containers" / CONTAINER / "manifest.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert any(
        entry["docType"] == "client_ingest"
        and entry.get("metadata", {}).get("taskId") == TASK_ID
        and PROOF_TEXT in entry["text"]
        for entry in entries
    )


def test_memory_objects_are_searchable_after_manifest_and_embed(tmp_path: Path, monkeypatch):
    workspace = create_proof_workspace(tmp_path)

    with fake_embedding_server() as base_url:
        server = load_server_module(
            workspace,
            monkeypatch,
            {
                "EMBEDDING_API_KEY": "fake-key",
                "EMBEDDINGS_BASE_URL": base_url,
            },
        )
        client = TestClient(server.app)

        ingest_response = client.post(
            "/ingest-memory/objects",
            headers={"X-API-KEY": API_KEY},
            json={
                "container": CONTAINER,
                "objects": [
                    {
                        "id": TASK_ID,
                        "text": PROOF_TEXT,
                        "title": "feat-004 typed ingest proof",
                        "source": "pytest",
                        "tags": ["feat-004", "typed-objects"],
                        "metadata": {"taskId": TASK_ID, "kind": "memory_object"},
                    }
                ],
            },
        )
        assert ingest_response.status_code == 200
        ingest_body = ingest_response.json()
        assert ingest_body["accepted"] == 1
        assert ingest_body["stored_paths"]

        post_ok(client, "/build-manifest", {"container": CONTAINER})
        post_ok(client, "/embed", {"container": CONTAINER})
        search_response = post_ok(
            client,
            "/search",
            {"container": CONTAINER, "query": QUERY_TEXT, "topk": 1},
        )

    assert search_response["results"]
    assert search_response["results"][0]["docType"] == "client_ingest"
    assert search_response["results"][0]["metadata"]["taskId"] == TASK_ID
    assert PROOF_TEXT in search_response["results"][0]["text"]
