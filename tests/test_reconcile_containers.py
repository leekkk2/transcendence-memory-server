import json
import lancedb
import numpy as np
import pyarrow as pa
import pytest
from argparse import Namespace
from scripts import reconcile_containers as merge


def source(root, name, identity, text='same'):
    p = root / 'tasks/rag/containers' / name
    p.mkdir(parents=True)
    (p/'memory_objects.jsonl').write_text(json.dumps({'id':identity,'text':text})+'\n')
    row = {'chunkId':identity+'#client-ingest#1','taskId':identity,'docType':'client_ingest','text':text,'vector':[0.0,1.0],
           'container':name,'sourcePath':f'tasks/rag/containers/{name}/memory_objects.jsonl'}
    schema = pa.schema([(k,pa.list_(pa.float32(),2) if k=='vector' else pa.string()) for k in row])
    lancedb.connect(str(p/'lancedb')).create_table('chunks',pa.Table.from_pylist([row],schema=schema))
    return p


def test_merge_reembeds_preserves_source_and_rejects_overwrite(tmp_path, monkeypatch):
    src, out = tmp_path/'src', tmp_path/'out'
    a=source(src,'old','1'); b=source(src,'main','2')
    before=[merge.digest(x/'memory_objects.jsonl') for x in [a,b]]
    monkeypatch.setattr(merge,'_build_embed_callable',lambda _: (lambda texts: np.array([[1.,0.]]*len(texts)),2,'test-model'))
    args=Namespace(source_workspace=src,output_workspace=out,sources=['old','main'],target='main',profile='test',batch_size=1,commit=True)
    merge.build(args)
    report=json.loads((out/'reconciliation.json').read_text())
    assert report['objects']==report['chunks']==2
    assert report['inconsistent_vectors']==2
    assert before==[merge.digest(x/'memory_objects.jsonl') for x in [a,b]]
    t=lancedb.connect(str(out/'tasks/rag/containers/main/lancedb')).open_table('chunks')
    assert t.schema.field('vector').type.list_size==2
    assert all(r['embedding_model']=='test-model' and r['vector']==[1.,0.] for r in t.to_arrow().to_pylist())
    with pytest.raises(ValueError,match='Output target exists'): merge.build(args)


def test_merge_repeated_ids_preserves_both_records(tmp_path):
    source(tmp_path,'a','1','first');source(tmp_path,'b','1','different')
    objects,chunks,_=merge.load_sources(tmp_path,['a','b'])
    assert len(objects)==len(chunks)==2
    assert len({r['chunkId'] for r in chunks})==2
    assert [r['text'] for r in objects]==['first','different']
