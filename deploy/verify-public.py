#!/usr/bin/env python3
import json,sys,time,urllib.request
base,revision=sys.argv[1:]
for attempt in range(12):
 try:
  def get(path):
   request=urllib.request.Request(base.rstrip('/')+path,headers={'User-Agent':'tm-deploy-verification/1','Cache-Control':'no-cache'})
   with urllib.request.urlopen(request,timeout=15) as response:return json.load(response)
  health=get('/health');cap=get('/capabilities')
  assert health['status']=='ok' and health['runtime_ready']['search']
  assert cap['source_revision']==revision
  print(json.dumps({'public_url':base,'revision':revision,'health':'ok','build_flavor':health['build_flavor']}));break
 except Exception:
  if attempt==11:raise SystemExit('Public health/revision verification failed')
  time.sleep(5)
