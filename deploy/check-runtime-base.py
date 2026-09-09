#!/usr/bin/env python3
"""Fail closed when dependencies no longer match the approved runtime base."""
import hashlib,json,pathlib,tomllib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def fingerprint(root=ROOT):
 project=tomllib.loads((root/'pyproject.toml').read_text(encoding='utf-8'))['project']
 data={'python':project['requires-python'],'dependencies':project['dependencies'],
       'multimodal':project.get('optional-dependencies',{}).get('multimodal',[]),
       'constraints':(root/'constraints.txt').read_text(encoding='utf-8'),
       'dockerfile':(root/'Dockerfile').read_text(encoding='utf-8')}
 return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
if __name__=='__main__':
 config=json.loads((ROOT/'deploy/runtime-base.json').read_text())
 if fingerprint()!=config['dependency_sha256']:
  raise SystemExit('Runtime dependencies changed. Build and validate a new full runtime, then update deploy/runtime-base.json.')
 if '@sha256:' not in config['image']:raise SystemExit('Runtime base must use a digest')
 print(config['image'])
