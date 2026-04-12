#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--container', default='smoke')
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / 'scripts'
    sys.path.insert(0, str(repo_root))

    with tempfile.TemporaryDirectory(prefix='tm-client-ingest-smoke-') as tmp:
        ws = Path(tmp)
        (ws / 'scripts').symlink_to(scripts_dir)
        os.environ['WORKSPACE'] = str(ws)
        os.environ['RAG_API_KEY'] = 'test-rag-key'

        import scripts.task_rag_server as server

        server.WS = ws
        server.SCRIPTS = ws / 'scripts'
        client = TestClient(server.app)

        ingest_response = client.post(
            '/ingest-memory/objects',
            headers={'X-API-KEY': os.environ['RAG_API_KEY']},
            json={
                'container': args.container,
                'objects': [
                    {
                        'id': 'obj-alpha',
                        'title': 'Alpha memory',
                        'text': 'alpha memory survives roundtrip search',
                        'source': 'smoke-test',
                        'tags': ['alpha', 'memory'],
                        'metadata': {'project': 'transcendence-memory', 'status': 'active'},
                    },
                    {
                        'id': 'obj-beta',
                        'title': 'Beta memory',
                        'text': 'beta note for contrast only',
                        'source': 'smoke-test',
                        'tags': ['beta'],
                        'metadata': {'project': 'transcendence-memory', 'status': 'active'},
                    },
                ],
            },
        )
        if ingest_response.status_code != 200:
            raise AssertionError(f'object ingest failed: {ingest_response.status_code} {ingest_response.text}')
        if ingest_response.json()['accepted'] != 2:
            raise AssertionError(f'unexpected ingest response: {ingest_response.json()}')

        stored_path = Path(ingest_response.json()['stored_path'])
        rows = [json.loads(line) for line in stored_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        if len(rows) != 2:
            raise AssertionError(f'expected 2 stored rows, got {len(rows)}')

        # 再写一个对照容器，用于验证模糊容器过滤
        sibling_container = f'{args.container}_sibling'
        sibling_resp = client.post(
            '/ingest-memory/objects',
            headers={'X-API-KEY': os.environ['RAG_API_KEY']},
            json={
                'container': sibling_container,
                'objects': [
                    {
                        'id': 'obj-sibling',
                        'title': 'Sibling memory',
                        'text': 'sibling container row',
                        'source': 'smoke-test',
                        'tags': ['sibling'],
                        'metadata': {'project': 'transcendence-memory'},
                    },
                ],
                'auto_embed': False,
            },
        )
        if sibling_resp.status_code != 200:
            raise AssertionError(f'sibling ingest failed: {sibling_resp.status_code} {sibling_resp.text}')

        # /containers 全量
        full = client.get('/containers', headers={'X-API-KEY': os.environ['RAG_API_KEY']})
        if full.status_code != 200:
            raise AssertionError(f'list containers failed: {full.status_code} {full.text}')
        all_names = {c['name'] for c in full.json()['containers']}
        if args.container not in all_names or sibling_container not in all_names:
            raise AssertionError(f'expected both containers in list, got {all_names}')

        # /containers?pattern= 子串过滤
        filtered = client.get(
            '/containers',
            params={'pattern': args.container},
            headers={'X-API-KEY': os.environ['RAG_API_KEY']},
        )
        if filtered.status_code != 200:
            raise AssertionError(f'pattern containers failed: {filtered.status_code} {filtered.text}')
        filtered_names = {c['name'] for c in filtered.json()['containers']}
        if not filtered_names.issuperset({args.container, sibling_container}):
            raise AssertionError(f'fuzzy filter missed expected entries: {filtered_names}')

        # 前缀模式 + 大小写不敏感
        prefix_resp = client.get(
            '/containers',
            params={'pattern': args.container.upper(), 'mode': 'prefix'},
            headers={'X-API-KEY': os.environ['RAG_API_KEY']},
        )
        if prefix_resp.status_code != 200:
            raise AssertionError(f'prefix mode failed: {prefix_resp.status_code} {prefix_resp.text}')
        prefix_names = {c['name'] for c in prefix_resp.json()['containers']}
        if args.container not in prefix_names:
            raise AssertionError(f'prefix mode should match {args.container}, got {prefix_names}')

        # /search container_pattern 命中 0 个时应返回 ok 空结果
        empty_search = client.post(
            '/search',
            headers={'X-API-KEY': os.environ['RAG_API_KEY']},
            json={'query': 'anything', 'container_pattern': '__definitely_no_match__'},
        )
        if empty_search.status_code != 200:
            raise AssertionError(f'empty pattern search HTTP failed: {empty_search.status_code} {empty_search.text}')
        empty_payload = empty_search.json()
        if empty_payload.get('status') != 'ok' or empty_payload.get('results'):
            raise AssertionError(f'empty pattern search should be ok+empty: {empty_payload}')
        if empty_payload.get('containers') != []:
            raise AssertionError(f'expected empty containers list, got {empty_payload.get("containers")}')

        print('smoke ok: ingest + fuzzy /containers + empty cross-container /search work')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
