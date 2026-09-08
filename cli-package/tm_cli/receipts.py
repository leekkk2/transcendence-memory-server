"""Bounded verification of an accepted write; no automatic write replay."""
import time
from .client import CLIError


def verify_receipt(client,receipt,container,obj,timeout):
    job=receipt.get('index_job_id')
    if job is None:
        raise CLIError('Write accepted but no index job receipt is available; inspect status before retrying.')
    deadline=time.monotonic()+timeout
    client.request_deadline=deadline
    while time.monotonic()<deadline:
        state=client.get(f'/jobs/{job}')
        if not state.get('running') and state.get('exit_code') is not None:
            if state['exit_code']!=0:raise CLIError('Write accepted; indexing failed. Do not resend the memory.')
            break
        time.sleep(min(1,max(0,deadline-time.monotonic())))
    else:raise CLIError('Write accepted; indexing still pending or unknown. Do not resend the memory.')
    data=client.post('/search',json_body={'container':container,'query':obj.get('title') or obj['text'][:2000],'topk':20,'union':False})
    found=any(hit.get('taskId')==obj['id'] for hit in data.get('results',[]))
    if not found:raise CLIError('Index job finished but original source not recalled; investigate without duplicating the write.')
    return {'accepted':True,'indexed':True,'retrievable':True,'job_id':job}


def persist_receipt(receipt,container,obj_id):
    import json,os
    from pathlib import Path
    from .permissions import private_directory
    folder=Path.home()/'.transcendence-memory/receipts'
    private_directory(folder)
    path=folder/(obj_id+'.json')
    with path.open('w',encoding='utf-8') as handle:
        if os.name!='nt':os.fchmod(handle.fileno(),0o600)
        json.dump({'container':container,'object_id':obj_id,'accepted':receipt.get('accepted'),
                   'index_job_id':receipt.get('index_job_id'),'index_status':receipt.get('index_status','unknown')},handle,ensure_ascii=False)
    return path
