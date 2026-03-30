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

        print('smoke ok: typed client objects persist to canonical memory_objects.jsonl')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
