import httpx,pytest
from tm_cli.client import Client,ServerError
from tm_cli.config import Settings
from tm_cli.receipts import verify_receipt
from tm_cli.client import CLIError


def test_safe_read_fallback_and_no_write_replay(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:3128')
    calls=[]
    def handle(req):
        calls.append(req.method)
        return httpx.Response(502,json={'error':'unavailable'})
    with Client(Settings(endpoint='https://example.invalid',api_key='fixture'),transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ServerError):client.post('/ingest-memory/objects',json_body={'objects':[]})
        assert calls==['POST']
        with pytest.raises(ServerError):client.post('/search',json_body={'query':'q'})
        assert calls==['POST','POST','POST']


def test_direct_mode_has_no_fallback(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid')
    calls=[]
    def handle(req):calls.append(req);return httpx.Response(503,json={})
    with Client(Settings(endpoint='https://example.invalid',transport_mode='direct'),transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ServerError):client.get('/health')
    assert len(calls)==1


def test_verification_requires_original_source():
    class Fake:
        def get(self,path):return {'running':False,'exit_code':0}
        def post(self,path,json_body):return {'results':[{'taskId':'another'}]}
    with pytest.raises(CLIError,match='original source'):
        verify_receipt(Fake(),{'index_job_id':1},'main',{'id':'original','text':'test'},5)


def test_socks_proxy_environment_can_construct_client(monkeypatch):
    monkeypatch.setenv('ALL_PROXY','socks5://127.0.0.1:19090')
    monkeypatch.delenv('HTTP_PROXY',raising=False)
    monkeypatch.delenv('HTTPS_PROXY',raising=False)
    with Client(Settings(endpoint='https://example.invalid')):
        pass
