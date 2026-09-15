"""Authenticated document browser contracts and browser upload CSRF boundary."""
import json
from fastapi.testclient import TestClient
import pytest
from conftest import API_KEY, load_server, make_workspace

HEADERS = {"X-API-KEY": API_KEY}
CSRF = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch, {"TM_ENV": "dev"})
    server._reset_ui_singletons()
    monkeypatch.setattr(server, "resolve_container_or_raise", lambda name: (name, None))
    path = workspace / "tasks/rag/containers/alpha/raganything"
    path.mkdir(parents=True)
    (path / "kv_store_doc_status.json").write_text(json.dumps({
        "doc-a": {"status": "processed", "content_summary": "alpha guide"},
        "doc-b": {"status": "failed", "error_msg": "upstream unavailable"},
    }))
    return server, TestClient(server.app), workspace


def test_document_admin_routes_require_auth(setup):
    _, client, _ = setup
    for path in ["/admin/documents/config", "/admin/containers/alpha/documents",
                 "/admin/containers/alpha/documents/doc-a", "/admin/containers/alpha/graph"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=HEADERS).status_code == 200


def test_cookie_session_can_read_and_logout_revokes(setup):
    _, client, _ = setup
    assert client.post("/admin/ui/login", json={"api_key": API_KEY}, headers=CSRF).status_code == 200
    assert client.get("/admin/containers/alpha/documents").json()["total"] == 2
    assert client.post("/admin/ui/logout", headers=CSRF).status_code == 200
    assert client.get("/admin/containers/alpha/documents").status_code == 401


def test_reader_filters_and_never_creates_missing_container(setup):
    _, client, workspace = setup
    response = client.get("/admin/containers/alpha/documents?status=failed&limit=1", headers=HEADERS)
    assert response.json()["documents"][0]["id"] == "doc-b"
    assert client.get("/admin/containers/missing/documents", headers=HEADERS).status_code == 404
    assert not (workspace / "tasks/rag/containers/missing").exists()
    for query in ["limit=0", "limit=101", "offset=-1", "q=" + "a" * 201]:
        assert client.get("/admin/containers/alpha/documents?" + query, headers=HEADERS).status_code == 422
    assert client.get("/admin/containers/alpha/documents/missing", headers=HEADERS).status_code == 404


def test_reader_rejects_invalid_and_symlinked_container(setup):
    _, client, workspace = setup
    root = workspace / "tasks/rag/containers"
    (root / "linked").symlink_to(root / "alpha")
    assert client.get("/admin/containers/linked/documents", headers=HEADERS).status_code == 400
    assert client.get("/admin/containers/bad%5Cname/documents", headers=HEADERS).status_code == 400


def test_corrupt_storage_is_503_not_empty_success(setup):
    _, client, workspace = setup
    path = workspace / "tasks/rag/containers/alpha/raganything/kv_store_doc_status.json"
    path.write_text("{partial")
    response = client.get("/admin/containers/alpha/documents", headers=HEADERS)
    assert response.status_code == 503
    assert str(workspace) not in response.text


def test_browser_multipart_and_text_upload_require_csrf_and_return_queued(setup, monkeypatch):
    server, client, _ = setup
    monkeypatch.setattr(server, "_require_lightrag_ready", lambda: None)
    monkeypatch.setattr(server, "get_raganything", object())
    assert client.post("/admin/ui/login", json={"api_key": API_KEY}, headers=CSRF).status_code == 200
    body = {"container": "alpha", "text": "Example guide content"}
    assert client.post("/documents/text", json=body).status_code == 400
    text = client.post("/documents/text", json=body, headers=CSRF)
    assert text.json()["status"] == "enqueued"
    assert client.post("/documents/upload", data={"container": "alpha"},
                       files={"file": ("test.pdf", b"%PDF-1.4 test")}).status_code == 400
    uploaded = client.post("/documents/upload", data={"container": "alpha"},
                           files={"file": ("test.pdf", b"%PDF-1.4 test")}, headers=CSRF)
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "enqueued"
    assert uploaded.json()["pid"] != text.json()["pid"]
    # Queued work does not manufacture a processed document.
    assert client.get("/admin/containers/alpha/documents").json()["total"] == 2
