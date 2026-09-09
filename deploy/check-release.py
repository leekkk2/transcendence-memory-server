#!/usr/bin/env python3
"""Only the current main commit with successful full CI may reach production."""
import json,os,re,sys,urllib.request

def valid_run(run,sha):
 return run.get('head_sha')==sha and run.get('head_branch')=='main' and run.get('event')=='push' and run.get('conclusion')=='success'
def check(sha,fetch):
 if not re.fullmatch(r'[0-9a-f]{40}',sha):raise ValueError('Expected a full commit SHA')
 if fetch('/git/ref/heads/main')['object']['sha']!=sha:raise ValueError('Stale deployment: main has advanced')
 runs=fetch('/actions/workflows/ci.yml/runs?branch=main&head_sha='+sha+'&per_page=100')['workflow_runs']
 if not any(valid_run(r,sha) for r in runs):raise ValueError('The exact main commit has not passed CI/CD')
if __name__=='__main__':
 repo=os.environ['GITHUB_REPOSITORY'];token=os.environ['GH_TOKEN']
 def fetch(path):
  req=urllib.request.Request('https://api.github.com/repos/'+repo+path,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json'})
  with urllib.request.urlopen(req,timeout=30) as response:return json.load(response)
 check(sys.argv[1],fetch);print('Current main and successful CI/CD verified')
