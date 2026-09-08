#!/usr/bin/env python3
"""Evaluate an existing isolated corpus; seeding/cleanup require explicit flags.
This synthetic seed validates the pipeline, not the quality of a user's main corpus.
"""
import argparse,json,os,time,statistics,math,hashlib
from pathlib import Path
import requests


def metrics(rows,k):
    positives=[r for r in rows if r['relevant']];negatives=[r for r in rows if not r['relevant']]
    return {'positive_cases':len(positives),'negative_cases':len(negatives),
            'recall_at_k':sum(r['found'] for r in positives)/len(positives) if positives else None,
            'mrr':sum(1/r['rank'] if r['rank'] else 0 for r in positives)/len(positives) if positives else None,
            'ndcg_at_k':sum(r['ndcg'] for r in positives)/len(positives) if positives else None,
            'rerank_coverage':sum(r['rerank_applied'] for r in rows)/len(rows) if rows else None,
            'negative_acceptance_rate':sum(r['accepted'] for r in negatives)/len(negatives) if negatives and all(r['accepted'] is not None and not r.get('error') for r in negatives) else None,
            'p50_ms':statistics.median([r['latency_ms'] for r in rows]) if rows else None,
            'p95_ms':sorted(r['latency_ms'] for r in rows)[max(0,math.ceil(len(rows)*.95)-1)] if rows else None,
            'degraded_cases':sum(r['degraded'] for r in rows),
            'error_cases':sum(bool(r.get('error')) for r in rows)}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--endpoint',required=True);p.add_argument('--container',required=True);p.add_argument('--dataset',type=Path,default=Path(__file__).resolve().parents[1]/'tests/fixtures/retrieval_eval/seed.json');p.add_argument('--output',type=Path,required=True);p.add_argument('--seed',action='store_true');p.add_argument('--cleanup',action='store_true');p.add_argument('--rerank',action='store_true');p.add_argument('--max-distance',type=float);p.add_argument('--topk',type=int,default=5);a=p.parse_args()
    if (a.seed or a.cleanup) and not a.container.startswith('tm-eval-'):p.error('Mutations require a disposable tm-eval- namespace')
    key=os.environ['TM_API_KEY'];session=requests.Session();session.headers.update({'X-API-KEY':key,'User-Agent':'tm-evaluation/1'})
    def call(path,body=None,method=None):
        r=session.request(method or ('POST' if body is not None else 'GET'),a.endpoint.rstrip('/')+path,json=body,timeout=90);r.raise_for_status();return r.json()
    dataset=json.loads(a.dataset.read_text());rows=[]
    capabilities=call('/capabilities')
    out={'dataset_version':dataset['dataset_version'],'dataset_sha256':hashlib.sha256(a.dataset.read_bytes()).hexdigest(),'scope':'isolated synthetic corpus','container':a.container,'rerank':a.rerank,'capabilities':capabilities,'distance_acceptance_threshold':a.max_distance,'cases':rows}
    a.output.parent.mkdir(parents=True,exist_ok=True)
    def save():
        out['splits']={split:metrics([r for r in rows if r['split']==split],a.topk) for split in ['calibration','holdout']}
        a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    save()
    if a.seed:
        existing=call('/containers')['containers']
        if any(c.get('name')==a.container or c.get('id')==a.container for c in existing):raise RuntimeError('Refusing to seed an existing namespace')
        call('/ingest-memory/objects',{'container':a.container,'objects':dataset['documents'],'auto_embed':False})
        job=call('/embed',{'container':a.container,'wait':False})['pid'];deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            state=call(f'/jobs/{job}')
            if state.get('exit_code') is not None:
                if state['exit_code']!=0:raise RuntimeError('Seed indexing failed')
                break
            time.sleep(1)
        else:raise TimeoutError('Seed indexing did not complete')
    for case in dataset['cases']:
        started=time.monotonic()
        try:
            result=call('/search',{'container':a.container,'query':case['query'],'topk':a.topk,'union':False,'rerank':a.rerank,'score_threshold':a.max_distance or 0})
            error='server_error' if result.get('status')=='error' else None
        except requests.RequestException as exc:
            result={};error=type(exc).__name__
        hits=[];seen=set()
        for hit in result.get('results',[]):
            if hit.get('taskId') in seen:continue
            seen.add(hit.get('taskId'));hits.append(hit)
        ranks=[i+1 for i,h in enumerate(hits) if h.get('taskId') in case['relevant_ids']]
        accepted=None if a.max_distance is None else any(h.get('score') is not None and h['score']<=a.max_distance for h in hits)
        rows.append({'id':case['id'],'error':error,'split':case['split'],'relevant':bool(case['relevant_ids']),'found':bool(ranks),'rank':min(ranks) if ranks else None,'ndcg':sum(1/math.log2(r+1) for r in ranks)/sum(1/math.log2(i+2) for i in range(min(a.topk,len(case['relevant_ids'])))) if case['relevant_ids'] else 0,'hits':[{'id':h.get('taskId'),'distance':h.get('score'),'rerank_score':h.get('rerank_score') if h.get('rerank_score') is not None else h.get('rerankScore')} for h in hits],'accepted':accepted,'latency_ms':round((time.monotonic()-started)*1000),'degraded':bool(result.get('degraded') or result.get('status')=='error'),'rerank_applied':result.get('rerank_applied',False)})
        save()
    print(json.dumps(out['splits'],ensure_ascii=False))
    if a.cleanup:
        call('/containers/'+a.container,method='DELETE');out['cleaned_up']=True;save()

if __name__=='__main__':main()
