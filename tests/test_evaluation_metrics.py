from scripts.evaluate_retrieval import metrics

def test_metrics_do_not_turn_missing_threshold_into_zero_false_positives():
    rows=[{'relevant':True,'found':True,'rank':2,'ndcg':0.63093,'accepted':None,'latency_ms':20,'degraded':False,'rerank_applied':True}, {'relevant':False,'found':False,'rank':None,'ndcg':0,'accepted':None,'latency_ms':30,'degraded':False,'rerank_applied':True}]
    d=metrics(rows,5)
    assert d['recall_at_k']==1 and d['mrr']==.5
    assert d['negative_acceptance_rate'] is None
    assert d['p95_ms']==30
