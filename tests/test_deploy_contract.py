import importlib.util,pathlib,json,pytest
ROOT=pathlib.Path(__file__).resolve().parents[1]
def load(name):
 s=importlib.util.spec_from_file_location(name,ROOT/'deploy'/name);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def test_only_successful_current_main_can_deploy():
 m=load('check-release.py');sha='a'*40
 state={'object':{'sha':sha}};runs={'workflow_runs':[{'head_sha':sha,'head_branch':'main','event':'push','conclusion':'success'}]}
 m.check(sha,lambda p:state if p.startswith('/git/') else runs)
 runs['workflow_runs'][0]['head_branch']='feature'
 with pytest.raises(ValueError):m.check(sha,lambda p:state if p.startswith('/git/') else runs)
 state['object']['sha']='b'*40
 with pytest.raises(ValueError,match='Stale'):m.check(sha,lambda p:state)

def test_promotion_failure_restores_config_and_recreates_old_service(tmp_path,monkeypatch):
 m=load('deploy-image.py');override=tmp_path/'docker-compose.override.yml';env=tmp_path/'.env'
 override.write_text('services:\n  rag-server:\n    image: old\n');env.write_text('TM_IMAGE=old\nOTHER=keep\n')
 original=[p.read_bytes() for p in [override,env]];calls=[];monkeypatch.setattr(m,'run',lambda c:calls.append(c))
 def failed():raise RuntimeError('new health failed')
 with pytest.raises(RuntimeError,match='new health'):
  m.promote(['docker','compose','-f',str(override)],'rag-server',override,env,'repo@sha256:'+'a'*64,failed)
 assert original==[p.read_bytes() for p in [override,env]]
 assert len(calls)==2 and all('--no-build' in call for call in calls)

def test_runtime_base_tracks_dependencies():
 m=load('check-runtime-base.py');config=json.loads((ROOT/'deploy/runtime-base.json').read_text())
 assert m.fingerprint()==config['dependency_sha256']
 assert '@sha256:' in config['image']
