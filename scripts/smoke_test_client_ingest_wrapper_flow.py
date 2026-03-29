#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path


def as_dict(value):
    return value.model_dump() if hasattr(value, 'model_dump') else value


def write_fake_scripts(scripts_dir: Path, real_scripts_dir: Path) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(real_scripts_dir / 'task_rag_build_manifest.py', scripts_dir / 'task_rag_build_manifest.py')
    shutil.copy2(real_scripts_dir / 'task_rag_ingest_memory_refs.py', scripts_dir / 'task_rag_ingest_memory_refs.py')

    embed_script = scripts_dir / 'task_rag_embed.py'
    embed_script.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', required=True)
    args = ap.parse_args()

    ws = Path(os.environ['WORKSPACE'])
    container_root = ws / 'tasks' / 'rag' / 'containers' / args.container
    client_files = sorted(container_root.glob('client-ingest-*.jsonl'))
    rows = []
    for path in client_files:
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            rows.append({
                'taskId': record['id'],
                'text': record['text'],
                'title': record.get('title'),
                'source': record.get('source'),
                'tags': record.get('tags', []),
                'metadata': record.get('metadata', {}),
            })

    (container_root / 'fake_faiss_docs.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')
    print(f'embedded {len(rows)} fake docs for {args.container}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
""",
        encoding='utf-8',
    )
    embed_script.chmod(embed_script.stat().st_mode | stat.S_IXUSR)

    search_script = scripts_dir / 'task_rag_search.py'
    search_script.write_text(
        """#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--query', required=True)
    ap.add_argument('--topk', type=int, default=5)
    ap.add_argument('--container', required=True)
    args = ap.parse_args()

    ws = Path(os.environ['WORKSPACE'])
    data_path = ws / 'tasks' / 'rag' / 'containers' / args.container / 'fake_faiss_docs.json'
    rows = json.loads(data_path.read_text(encoding='utf-8')) if data_path.exists() else []
    query_terms = {part.lower() for part in args.query.split() if part.strip()}

    scored = []
    for row in rows:
        haystack = ' '.join([
            row.get('taskId', ''),
            row.get('title') or '',
            row.get('text') or '',
            row.get('source') or '',
            ' '.join(row.get('tags', [])),
            json.dumps(row.get('metadata', {}), ensure_ascii=False),
        ]).lower()
        score = sum(1 for term in query_terms if term in haystack)
        if score:
            scored.append({'score': score, **row})

    scored.sort(key=lambda item: (-item['score'], item.get('taskId', '')))
    print(json.dumps(scored[: args.topk], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
""",
        encoding='utf-8',
    )
    search_script.chmod(search_script.stat().st_mode | stat.S_IXUSR)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='smoke-wrapper')
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    real_scripts_dir = repo_root / 'scripts'
    sys.path.insert(0, str(real_scripts_dir))

    with tempfile.TemporaryDirectory(prefix='tm-client-ingest-wrapper-') as tmp:
        ws = Path(tmp)
        os.environ['WORKSPACE'] = str(ws)
        os.environ['RAG_API_KEY'] = 'test-rag-key'
        os.environ['EMBEDDING_API_KEY'] = 'test-embedding-key'

        import task_rag_server as server
        import task_rag_build_manifest as build_manifest_mod

        server.WS = ws
        build_manifest_mod.WS = ws
        build_manifest_mod.TASKS = ws / 'tasks'

        fake_scripts_dir = ws / 'scripts'
        server.SCRIPTS = fake_scripts_dir
        write_fake_scripts(fake_scripts_dir, real_scripts_dir)

        ingest_resp = as_dict(
            server.ingest_objects(
                server.ClientIngestReq(
                    container=args.container,
                    objects=[
                        server.IngestObject(
                            id='obj-alpha',
                            title='Alpha memory',
                            text='alpha memory survives wrapper flow search',
                            source='wrapper-smoke',
                            tags=['alpha', 'memory'],
                            metadata={'project': 'transcendence-memory', 'status': 'active'},
                        ),
                        server.IngestObject(
                            id='obj-beta',
                            title='Beta memory',
                            text='beta note for contrast only',
                            source='wrapper-smoke',
                            tags=['beta'],
                            metadata={'project': 'transcendence-memory', 'status': 'active'},
                        ),
                    ],
                )
            )
        )
        if ingest_resp['accepted'] != 2:
            raise AssertionError(f"expected 2 accepted objects, got {ingest_resp['accepted']}")

        old_argv = sys.argv[:]
        try:
            sys.argv = ['task_rag_build_manifest.py', '--container', args.container]
            build_manifest_mod.main()
        finally:
            sys.argv = old_argv

        build_resp = as_dict(server.build_manifest(server.ContainerReq(container=args.container)))
        if build_resp['code'] != 0:
            raise AssertionError(f'build-manifest wrapper failed: {build_resp}')

        embed_resp = as_dict(server.embed(server.ContainerReq(container=args.container)))
        if embed_resp['code'] != 0:
            raise AssertionError(f'embed wrapper failed: {embed_resp}')
        if 'embedded 2 fake docs' not in embed_resp['stdout']:
            raise AssertionError(f'unexpected embed stdout: {embed_resp["stdout"]!r}')

        health_resp = as_dict(server.health())
        if not health_resp['runtime_ready']['ingest_objects']:
            raise AssertionError(f"expected ingest_objects runtime readiness, got {health_resp['runtime_ready']}")
        if health_resp['endpoint_contracts']['ingest_objects'] != 'typed-json-response':
            raise AssertionError(f"unexpected ingest_objects contract: {health_resp['endpoint_contracts']}")
        if health_resp['server_architecture']['retrieval_mode'] != 'server-side':
            raise AssertionError(f"unexpected server architecture: {health_resp['server_architecture']}")

        search_resp = as_dict(server.search(server.SearchReq(query='alpha memory', topk=2, container=args.container)))
        if search_resp['code'] != 0:
            raise AssertionError(f'search wrapper failed: {search_resp}')

        results = json.loads(search_resp['stdout'])
        if not results:
            raise AssertionError('search returned no wrapper results')
        if results[0].get('taskId') != 'obj-alpha':
            raise AssertionError(f'expected obj-alpha as top hit, got {results[0]}')

        print('smoke ok: wrapper build-manifest/embed/search path returns obj-alpha for client-ingested content')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
