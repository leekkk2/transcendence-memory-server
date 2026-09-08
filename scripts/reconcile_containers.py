#!/usr/bin/env python3
"""Build a verified merged container in an offline workspace; never mutate sources.

Requires a stopped-service snapshot as input. Re-embed EVERY indexed chunk with
one explicit profile (no fallback), since historical profile labels may be false.
The operator verifies source hashes again during cutover. JSONL is authoritative;
unknown/unindexed objects are rejected rather than silently omitted.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

import lancedb
import numpy as np
import pyarrow as pa

sys.path.insert(0, str(Path(__file__).resolve().parent))
from migrate_embeddings import _build_embed_callable


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_sources(root, sources):
    objects, chunks, manifest = [], [], {}
    for name in sources:
        directory = root / 'tasks/rag/containers' / name
        source = directory / 'memory_objects.jsonl'
        rows = [json.loads(line) for line in source.read_text(encoding='utf-8').splitlines() if line.strip()]
        table = lancedb.connect(str(directory / 'lancedb')).open_table('chunks')
        indexed = table.to_arrow().to_pylist()
        manifest[name] = {'sha256': digest(source), 'objects': len(rows), 'chunks': len(indexed)}
        offset = len(objects)
        expected = {f"{r['id']}#client-ingest#{i}" for i, r in enumerate(rows, 1)}
        actual = {r['chunkId'] for r in indexed if r.get('docType') == 'client_ingest'}
        if expected != actual:
            raise ValueError(f'{name}: source lines and indexed chunks differ; finish indexing first')
        # Historical repeated memory IDs are distinct JSONL lines. Preserve all
        # records; line-based chunk IDs keep them distinct after concatenation.
        for row in indexed:
            if row.get('docType') == 'client_ingest':
                head, line = row['chunkId'].rsplit('#', 1)
                row['chunkId'] = f'{head}#{int(line)+offset}'
            elif any(c['chunkId'] == row['chunkId'] for c in chunks):
                raise ValueError('Non-memory chunk collision requires explicit reconciliation')
            chunks.append(row)
        objects.extend(rows)
    if len({r['chunkId'] for r in chunks}) != len(chunks):
        raise ValueError('Duplicate chunk IDs after reconciliation')
    return objects, chunks, manifest


def build(args):
    root, output = Path(args.source_workspace).resolve(), Path(args.output_workspace).resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError('Source and output workspaces must be separate')
    if any('/' in n or n in ('.', '..') for n in [*args.sources, args.target]):
        raise ValueError('Invalid container name')
    objects, chunks, manifest = load_sources(root, args.sources)
    report = {'sources': manifest, 'target': args.target, 'objects': len(objects),
              'chunks': len(chunks), 'profile': args.profile,
              'input_chars': os.environ.get('TM_EMBEDDING_MAX_INPUT_CHARS','0'), 'status': 'planned'}
    if not args.commit:
        print(json.dumps(report, indent=2)); return
    target = output / 'tasks/rag/containers' / args.target
    checkpoint = output / 'reconciliation-plan.json'
    if target.exists() and not getattr(args, 'resume', False):
        raise ValueError('Output target exists; retain it for inspection and use a new output workspace')
    if target.exists() and (not checkpoint.exists() or json.loads(checkpoint.read_text(encoding='utf-8')) != report):
        raise ValueError('Resume plan does not match source hashes, profile or input strategy')
    # Retain graph, governance snapshots and source artifacts from the primary.
    if not target.exists():
        shutil.copytree(root / 'tasks/rag/containers' / args.sources[0], target,
                        ignore=shutil.ignore_patterns('lancedb', 'memory_objects.jsonl'))
        checkpoint.write_text(json.dumps(report,indent=2))
    for name in args.sources[1:]:
        secondary = root / 'tasks/rag/containers' / name
        if (secondary / 'raganything').exists():
            raise ValueError('Secondary has a knowledge graph: merge graph explicitly before cutover')
    source = target / 'memory_objects.jsonl'
    source.write_bytes(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in objects).encode('utf-8'))
    embed, dim, model = _build_embed_callable(args.profile)
    db = lancedb.connect(str(target / 'lancedb'))
    table = db.open_table('chunks') if (target/'lancedb/chunks.lance').exists() else None
    completed = table.count_rows() if table is not None else 0
    if table is not None:
        existing = table.to_arrow().to_pylist()
        if [r['chunkId'] for r in existing] != [r['chunkId'] for r in chunks[:completed]]:
            raise ValueError('Resume indexed chunk sequence differs from source')
    minimum_cosine, replaced = 1.0, 0
    started = time.time()
    for offset in range(completed, len(chunks), args.batch_size):
        batch = chunks[offset:offset+args.batch_size]
        vectors = embed([r['text'] for r in batch])
        if vectors.shape != (len(batch), dim) or not np.isfinite(vectors).all():
            raise ValueError('Embedding response shape or finite-value validation failed')
        for row, vector in zip(batch, vectors):
            old = np.asarray(row['vector'])
            cosine = float(np.dot(old, vector)/(np.linalg.norm(old)*np.linalg.norm(vector)))
            minimum_cosine = min(minimum_cosine, cosine)
            replaced += int(cosine < 0.999)
            row.update(vector=vector.tolist(), container=args.target, embedding_model=model,
                       embedding_dim=dim, embedding_profile=args.profile, embedded_at=int(time.time()))
            for name in args.sources:
                row['sourcePath'] = row.get('sourcePath', '').replace(
                    f'containers/{name}/', f'containers/{args.target}/')
        arrow = pa.Table.from_pylist(batch)
        arrow = arrow.set_column(arrow.schema.get_field_index('vector'), 'vector',
                                pa.array([r['vector'] for r in batch], type=pa.list_(pa.float32(), dim)))
        if table is None:
            table = db.create_table('chunks', data=arrow)
        else:
            table.add(arrow)
        print(json.dumps({'indexed': offset+len(batch), 'total': len(chunks),
                          'elapsed_s': round(time.time()-started), 'inconsistent_vectors': replaced}), flush=True)
    assert table.count_rows() == len(chunks)
    assert digest(source) == hashlib.sha256(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in objects).encode()).hexdigest()
    report.update(status='verified', model=model, dim=dim, minimum_old_cosine=minimum_cosine,
                  inconsistent_vectors=replaced, target_sha256=digest(source), elapsed_s=round(time.time()-started))
    (output / 'reconciliation.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-workspace', required=True)
    parser.add_argument('--output-workspace', required=True)
    parser.add_argument('--sources', nargs='+', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--batch-size', type=int, default=16)
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 100:
        parser.error('batch-size must be 1..100')
    build(args)
