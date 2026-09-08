from fastapi.testclient import TestClient
from conftest import load_server,make_workspace,auth_headers


def test_receipt_preserves_accepted_and_reports_queue_failure(tmp_path,monkeypatch):
    server=load_server(make_workspace(tmp_path),monkeypatch)
    class Broken:
        def enqueue(self,**kw):raise OSError('store unavailable')
    monkeypatch.setattr(server,'get_job_queue',lambda:Broken())
    with TestClient(server.app) as c:
        body=c.post('/ingest-memory/objects',headers=auth_headers(),json={'container':'test','objects':[{'id':'one','text':'hello'}]}).json()
    assert body['accepted']==1 and body['object_ids']==['one']
    assert body['index_status']=='unavailable' and body['index_job_id'] is None


def test_capability_identity_has_no_secret(tmp_path,monkeypatch):
    server=load_server(make_workspace(tmp_path),monkeypatch)
    with TestClient(server.app) as c:body=c.get('/capabilities').json()
    assert body['contract_version']=='2026-09-09'
    assert body['score_contract']['distance_metric']=='l2_squared'
    assert 'test-rag-key' not in str(body)
