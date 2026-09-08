import asyncio
import math
import pytest
from scripts.task_rag_server_models import SearchHit


def test_score_aliases_preserve_zero_and_legacy_value():
    hit=SearchHit(score=0.0,vectorScore=0.0,rerankScore=0.0)
    d=hit.model_dump()
    assert d['score']==d['vectorScore']==d['vector_distance']==0
    assert d['rerank_score']==0
    assert d['distance_metric']=='l2_squared'
    hit.rerankScore=-3
    assert hit.model_dump()['rerank_score']==-3


def test_score_cannot_serialize_nan():
    with pytest.raises(ValueError):SearchHit(score=float('nan'))


def test_actual_lance_metric_is_squared_l2(tmp_path):
    import lancedb,pyarrow as pa
    table=lancedb.connect(str(tmp_path/'db')).create_table('vectors',pa.table({'id':['x'],'vector':pa.array([[0.,0.]],type=pa.list_(pa.float32(),2))}))
    result=table.search([3.,4.]).metric('l2').limit(1).to_list()
    assert result[0]['_distance']==pytest.approx(25.)
