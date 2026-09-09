#!/usr/bin/env python3
"""Pull, isolate, promote and verify one Compose service, with config rollback.
Never builds on the production host and never prunes unrelated containers/images.
"""
import argparse,contextlib,hashlib,json,os,pathlib,re,shutil,subprocess,time,urllib.request
import yaml

def run(args,check=True):
 r=subprocess.run(args,capture_output=True,text=True)
 if check and r.returncode:raise RuntimeError('Command failed: '+args[0]+' '+args[1])
 return r.stdout

def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def get(base,path,key=None,payload=None):
 headers={'User-Agent':'tm-deploy/1','Cache-Control':'no-cache'}
 if key:headers['X-API-KEY']=key
 if payload is not None:headers['Content-Type']='application/json'
 with urllib.request.urlopen(urllib.request.Request(base+path,headers=headers,data=json.dumps(payload).encode() if payload is not None else None),timeout=30) as response:return json.load(response)
def validate_target(source,project,service_dir):
 labels=source['Config'].get('Labels',{})
 if labels.get('com.docker.compose.project')!=project:raise ValueError('Compose project mismatch')
 if pathlib.Path(labels.get('com.docker.compose.project.working_dir','')).resolve()!=service_dir.resolve():raise ValueError('Compose directory mismatch')
 for m in source['Mounts']:
  if m['Destination'].startswith('/app/') and not m['Destination'].startswith('/app/config'):
   raise ValueError('Source bind mount would hide the release image')
def wait_ready(name,base,revision,key=None,docker_health=False):
 deadline=time.monotonic()+180
 while time.monotonic()<deadline:
  try:
   h=get(base,'/health',key);c=get(base,'/capabilities',key)
   state=inspect(name)['State']
   if h.get('runtime_ready',{}).get('search') and c.get('source_revision')==revision and (not docker_health or state.get('Health',{}).get('Status')=='healthy'):
    return c
  except Exception:pass
  time.sleep(2)
 raise RuntimeError('Health/revision verification timed out')
def restore_files(saved):
 for path,content in saved.items():
  if content is None:
   if path.exists():path.unlink()
  else:path.write_bytes(content);path.chmod(0o600)
def promote(compose,service,override,envfile,image,verify):
 saved={p:p.read_bytes() if p.exists() else None for p in [override,envfile]}
 try:
  conf=yaml.safe_load(override.read_text()) if override.exists() else {};conf=conf or {}
  conf.setdefault('services',{}).setdefault(service,{})['image']=image
  override.write_text(yaml.safe_dump(conf,sort_keys=False));override.chmod(0o600)
  raw=envfile.read_text() if envfile.exists() else ''
  lines=[line for line in raw.splitlines() if not line.startswith('TM_IMAGE=')]
  envfile.write_text('\n'.join(lines)+f'\nTM_IMAGE={image}\n');envfile.chmod(0o600)
  run(compose+['up','-d','--no-deps','--no-build','--force-recreate','--pull','never',service])
  return verify()
 except Exception:
  restore_files(saved)
  old=compose[:]
  if saved[override] is None:
   i=old.index(str(override));del old[i-1:i+1]
  run(old+['up','-d','--no-deps','--no-build','--force-recreate','--pull','never',service])
  raise

def main():
 p=argparse.ArgumentParser(description=__doc__)
 for name in ['image','revision','container','project','service-dir','public-url']:p.add_argument('--'+name,required=True)
 p.add_argument('--expected-image');p.add_argument('--dry-run',action='store_true');a=p.parse_args()
 if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}',a.image):p.error('Pinned image digest required')
 if not re.fullmatch(r'[0-9a-f]{40}',a.revision):p.error('Full source revision required')
 work=pathlib.Path(a.service_dir).resolve();source=inspect(a.container);validate_target(source,a.project,work)
 if a.expected_image and source['Config']['Image']!=a.expected_image:raise RuntimeError('Concurrent image change; rebase first')
 if a.dry_run:print(json.dumps({'project':a.project,'current_image':source['Config']['Image'],'target_image':a.image,'revision':a.revision,'dry_run':True}));return
 import fcntl
 root=work/'.deployments';root.mkdir(exist_ok=True,mode=0o700)
 with (root/'lock').open('w') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  record=root/(time.strftime('%Y%m%dT%H%M%S')+'-'+a.revision[:12]);record.mkdir(mode=0o700)
  env=dict(v.split('=',1) for v in source['Config']['Env']);key=env.get('RAG_API_KEY')
  port=source['HostConfig']['PortBindings']['8711/tcp'][0]['HostPort'];base='http://127.0.0.1:'+port
  before=get(base,'/containers',key)['containers']
  run(['docker','pull',a.image])
  candidate='tm-release-check-'+str(time.time_ns());envpath=record/'candidate.env'
  candidate_env={**env,'WORKSPACE':'/data','TM_DISABLE_WORKER':'1','TM_REDIS_ENABLED':'0'}
  if any('\n' in v for v in candidate_env.values()):raise ValueError('Multiline environment requires explicit handling')
  envpath.write_text(''.join(k+'='+v+'\n' for k,v in candidate_env.items()));envpath.chmod(0o600)
  try:
   command=['docker','run','-d','--name',candidate,'--network',next(iter(source['NetworkSettings']['Networks'])),'--env-file',str(envpath),'-p','127.0.0.1::8711','-v',candidate+'-data:/data']
   for m in source['Mounts']:
    if m['Destination'].startswith('/app/config'):command+=['-v',m['Source']+':'+m['Destination']+':ro']
   run(command+[a.image]);cp=inspect(candidate)['NetworkSettings']['Ports']['8711/tcp'][0]['HostPort']
   wait_ready(candidate,'http://127.0.0.1:'+cp,a.revision,key)
  finally:
   run(['docker','rm','-f',candidate],False);run(['docker','volume','rm',candidate+'-data'],False);envpath.unlink(missing_ok=True)
  deadline=time.monotonic()+120
  while get(base,'/jobs?status=running&limit=100',key)['jobs']:
   if time.monotonic()>deadline:raise RuntimeError('Active jobs did not drain; production unchanged')
   time.sleep(2)
  if inspect(a.container)['Image']!=source['Image']:raise RuntimeError('Concurrent deployment detected; production unchanged')
  labels=source['Config']['Labels'];service=labels['com.docker.compose.service']
  override=work/'docker-compose.override.yml';envfile=work/'.env'
  for path in [override,envfile]:
   if path.exists():shutil.copy2(path,record/path.name);(record/path.name).chmod(0o600)
  compose=['docker','compose','-p',a.project,'--project-directory',str(work)]
  files=labels['com.docker.compose.project.config_files'].split(',')
  if str(override) not in files:files.append(str(override))
  for path in files:compose+=['-f',path]
  def verify():
   cap=wait_ready(a.container,base,a.revision,key,True)
   after=get(base,'/containers',key)['containers'];mapping={c['id']:c for c in after}
   if any(c['id'] not in mapping or mapping[c['id']].get('objects',0)<c.get('objects',0) for c in before):raise RuntimeError('Container object count regressed')
   pub=get(a.public_url.rstrip('/'),'/capabilities')
   if pub['source_revision']!=a.revision:raise RuntimeError('Public route is not serving the new revision')
   main=next((c for c in after if c['id']=='main'),None)
   if main and main.get('objects',0)>0:
    result=get(a.public_url.rstrip('/'),'/search',key,{'container':'main','query':'transcendence memory configuration','topk':1,'union':False,'rerank':False,'score_threshold':0})
    if result.get('status')!='ok' or result.get('degraded') or not result.get('results'):raise RuntimeError('Public main retrieval verification failed')
   return {'capabilities':cap,'public_retrieval':'verified' if main and main.get('objects',0)>0 else 'empty_corpus','containers':[{k:c.get(k) for k in ['id','objects','index_state']} for c in after]}
  receipt={'image':a.image,'revision':a.revision,'previous_image':source['Config']['Image'],'project':a.project,'backup':str(record)}
  try:receipt.update(status='verified',**promote(compose,service,override,envfile,a.image,verify))
  except Exception:
   receipt['status']='rolled_back';(record/'release.json').write_text(json.dumps(receipt,indent=2));raise
  (record/'release.json').write_text(json.dumps(receipt,indent=2));(root/'latest.json').write_text(json.dumps(receipt,indent=2))
  print(json.dumps(receipt))
if __name__=='__main__':main()
