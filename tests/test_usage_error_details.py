import json
import sqlite3
import time

from scripts import usage_analytics as usage


def test_upgrade_existing_log_and_classify_without_erasing(tmp_path):
    db = tmp_path / 'queue.db'
    usage.ensure_schema(db)
    with sqlite3.connect(db) as conn:
        cols = {r[1] for r in conn.execute('pragma table_info(api_request_log)')}
    assert {'error_detail', 'request_id'} <= cols
    batch = usage.UsageBatcher(db)
    for status, key, detail in [(404, None, None), (401, None, None), (410, 'hashed', 'container removed')]:
        batch._write_batch([dict(ts=int(time.time()*1000), method='GET', path='/search', status=status,
            latency_ms=1, container='old', api_key_hash=key, ua='test', bytes_in=0, bytes_out=0,
            error_type=usage.classify_error(status), error_detail=detail, request_id='request-test')])
    s = usage.summary(db)
    assert s['total_errors'] == 3
    assert s['unauthenticated_not_found'] == 1
    assert s['authenticated_errors'] == 1
    result = usage.errors(db, category='authenticated', limit=1)
    assert result['total'] == 1
    assert result['rows'][0]['error_detail'] == 'container removed'
    assert usage.errors(db, category='all')['total'] == 3


def test_error_redaction_drops_validation_inputs_and_credentials():
    raw = {'detail':[{'loc':['body','token'], 'msg':'invalid value', 'type':'value_error',
                      'input':'private-memory-content', 'ctx':{'secret':'private-memory-content'}}],
           'api_key':'credential-value'}
    detail = usage.safe_error_detail(raw)
    assert 'private-memory-content' not in detail
    assert 'credential-value' not in detail
    assert 'invalid value' in detail
    assert 'sk-secret123456789' not in usage.safe_error_detail({'detail':'key sk-secret123456789 rejected'})


def test_error_response_preserved_and_logged(tmp_path):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    db = tmp_path / 'queue.db'
    usage.ensure_schema(db)
    batch = usage.UsageBatcher(db)
    app = FastAPI()
    app.add_middleware(usage.UsageMiddleware, db_path=db, batcher=batch)
    @app.get('/failed')
    def failed():
        raise HTTPException(410, detail={'error':'container_removed','hint':'use configured container'})
    with TestClient(app) as client:
        response = client.get('/failed', headers={'X-API-KEY':'test-key'})
        assert response.status_code == 410
        assert response.json()['detail']['error'] == 'container_removed'
        assert response.headers['x-request-id']
        import asyncio
        asyncio.run(batch.flush())
    row = usage.errors(db)['rows'][0]
    assert 'container_removed' in row['error_detail']
    assert row['request_id'] == response.headers['x-request-id']


def test_errors_endpoint_requires_auth_and_returns_rows(tmp_path, monkeypatch):
    from conftest import load_server, make_workspace, auth_headers
    from fastapi.testclient import TestClient
    server=load_server(make_workspace(tmp_path),monkeypatch)
    with TestClient(server.app) as client:
        assert client.get('/admin/usage/errors').status_code==401
        response=client.get('/admin/usage/errors',headers=auth_headers())
        assert response.status_code==200
        assert 'rows' in response.json()


def test_tool_business_failure_has_details(tmp_path):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    db=tmp_path/'queue.db'
    usage.ensure_schema(db)
    batch=usage.UsageBatcher(db)
    app=FastAPI();app.add_middleware(usage.UsageMiddleware,db_path=db,batcher=batch)
    @app.post('/tool')
    def tool(request: Request):
        request.state.usage_error={'error':'gateway failed','notes':'model returned 401'}
        return {'status':'error'}
    with TestClient(app) as client:
        assert client.post('/tool',json={},headers={'X-API-KEY':'test-key'}).status_code==200
        import asyncio
        asyncio.run(batch.flush())
    row=usage.errors(db)['rows'][0]
    assert row['status']==200 and row['error_type']=='tool'
    assert 'gateway failed' in row['error_detail']
    assert usage.summary(db)['total_errors']==1


def test_container_switch_api_persists(tmp_path,monkeypatch):
    from conftest import load_server,make_workspace,auth_headers
    from fastapi.testclient import TestClient
    server=load_server(make_workspace(tmp_path),monkeypatch)
    key='config:tools:container:main:enabled_map'
    with TestClient(server.app) as client:
        result=client.put('/admin/config',headers=auth_headers(),json={'updates':[{'key':key,'value':{'compress_knowledge_cluster':True}}]})
        assert result.json()['applied']==1,result.json()
        import asyncio
        assert asyncio.run(server.governance_tools.read_container_raw_map('main'))=={'compress_knowledge_cluster':True}
