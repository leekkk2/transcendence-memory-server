#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='smoke')
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / 'scripts'
    sys.path.insert(0, str(scripts_dir))

    with tempfile.TemporaryDirectory(prefix='tm-client-ingest-smoke-') as tmp:
        ws = Path(tmp)
        os.environ['WORKSPACE'] = str(ws)
        os.environ['RAG_API_KEY'] = 'test-rag-key'
        os.environ['EMBEDDING_API_KEY'] = 'test-embedding-key'

        import task_rag_server as server
        import task_rag_build_manifest as build_manifest_mod
        import task_rag_embed as embed_mod
        import task_rag_search as search_mod

        server.WS = ws
        server.SCRIPTS = scripts_dir
        build_manifest_mod.WS = ws
        build_manifest_mod.TASKS = ws / 'tasks'
        embed_mod.WS = ws
        embed_mod.TASKS = ws / 'tasks'
        search_mod.WS = ws
        search_mod.TASKS = ws / 'tasks'

        def fake_embed_text(text: str) -> np.ndarray:
            text = text.lower()
            return np.array(
                [
                    float('alpha' in text),
                    float('beta' in text),
                    float('memory' in text),
                ],
                dtype='float32',
            )

        embed_mod.embed_text = fake_embed_text
        search_mod.embed_text = fake_embed_text

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
            raise AssertionError(
                f"expected POST /ingest-memory/objects to succeed, "
                f"got {ingest_response.status_code}: {ingest_response.text}"
            )
        ingest_resp = ingest_response.json()
        if ingest_resp['accepted'] != 2:
            raise AssertionError(f"expected 2 accepted objects, got {ingest_resp['accepted']}")

        old_argv = sys.argv[:]
        try:
            sys.argv = ['task_rag_build_manifest.py', '--container', args.container]
            with redirect_stdout(io.StringIO()):
                build_manifest_mod.main()

            manifest_path = ws / 'tasks' / 'rag' / 'containers' / args.container / 'manifest.jsonl'
            manifest_rows = [json.loads(line) for line in manifest_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            if not any(row.get('docType') == 'client_ingest' and row.get('taskId') == 'obj-alpha' for row in manifest_rows):
                raise AssertionError('client_ingest obj-alpha missing from manifest')

            sys.argv = ['task_rag_embed.py', '--container', args.container]
            with redirect_stdout(io.StringIO()) as embed_stdout:
                embed_mod.main()
            if 'embedded 2' not in embed_stdout.getvalue():
                raise AssertionError(f'unexpected embed output: {embed_stdout.getvalue()!r}')

            results = search_mod.search_faiss('alpha memory', 2, search_mod.container_dir(args.container))
            if not results:
                raise AssertionError('search returned no results')
            top = results[0]
            if top.get('taskId') != 'obj-alpha':
                raise AssertionError(f'expected obj-alpha as top hit, got {top}')
        finally:
            sys.argv = old_argv

        print('smoke ok: client ingest objects become searchable after build-manifest + embed')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
