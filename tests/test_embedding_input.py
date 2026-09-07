import asyncio
import numpy as np
from scripts.embedding_input import parts,pool,embed_bounded


def test_long_input_keeps_every_character_and_limits_batches(monkeypatch):
    monkeypatch.setenv('TM_EMBEDDING_MAX_INPUT_CHARS','3')
    monkeypatch.setenv('TM_EMBEDDING_MAX_BATCH_SIZE','2')
    calls=[]
    async def call(texts):
        assert len(texts)<=2 and all(len(t)<=3 for t in texts)
        calls.extend(texts)
        return np.array([[len(t),1] for t in texts],dtype=np.float32)
    result=asyncio.run(embed_bounded(['你好世界abc','z'],call))
    assert ''.join(calls)=='你好世界abcz'
    assert result.shape==(2,2)
    assert np.isclose(np.linalg.norm(result[0]),1)
    assert np.allclose(result[1],[1,1])


def test_pooling_weighted_and_single_unchanged():
    assert parts('abcdef',2)==['ab','cd','ef']
    assert np.allclose(pool([[1,0],[0,1]],['abc','d']),np.array([3,1])/np.sqrt(10))
    assert np.allclose(pool([[2,0]],['abc']),[2,0])


def test_sync_and_async_apply_identical_transform(monkeypatch):
    from scripts import embedding_registry as er, task_rag_runtime as runtime
    monkeypatch.setenv('TM_EMBEDDING_MAX_INPUT_CHARS','3')
    monkeypatch.setenv('TM_EMBEDDING_MAX_BATCH_SIZE','2')
    def value(text):return np.array([len(text),sum(ord(c) for c in text)%7+1],dtype=np.float32)
    async def request(profile,texts):return np.array([value(t) for t in texts])
    monkeypatch.setattr(er,'_http_embed_request',request)
    monkeypatch.setattr(runtime,'_embed_text_request',lambda p,t,m,title:value(t))
    text='中文测试abcdef'
    assert np.allclose(asyncio.run(er._http_embed_single(None,[text]))[0],runtime._embed_text_single(None,text,'document',None))
