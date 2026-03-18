#!/usr/bin/env python3
"""Search task RAG stores (FAISS or SQLite fallback).

Usage:
  task_rag_search.py --query "..." --topk 5
"""
from __future__ import annotations
import argparse
import json
import os
import sqlite3
from pathlib import Path
import requests
import numpy as np

try:
    import faiss  # type: ignore
except Exception:
    faiss = None

import os

WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'


def container_dir(container: str):
    base = TASKS / 'rag' / 'containers' / container
    base.mkdir(parents=True, exist_ok=True)
    return base

API_KEY = os.getenv('EMBEDDING_API_KEY', '')
MODEL = os.getenv('EMBEDDING_MODEL', 'gemini-embedding-001')
EMBEDDINGS_BASE_URL = os.getenv('EMBEDDINGS_BASE_URL', 'https://newapi.zweiteng.tk/v1')
GOOGLE_BASE_URL = os.getenv('GOOGLE_EMBEDDING_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/models')


def embed_text(text: str):
    if not API_KEY:
        raise RuntimeError('EMBEDDING_API_KEY not set')

    try:
        url = f"{EMBEDDINGS_BASE_URL.rstrip('/')}/embeddings"
        headers = {
            'Authorization': f'Bearer {API_KEY}',
            'Content-Type': 'application/json'
        }
        payload = {"model": MODEL, "input": text}
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        vec = data['data'][0]['embedding']
        return np.array(vec, dtype='float32')
    except Exception:
        url = f"{GOOGLE_BASE_URL.rstrip('/')}/{MODEL}:embedContent?key={API_KEY}"
        payload = {"content": {"parts": [{"text": text}]}}
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        vec = data['embedding']['values']
        return np.array(vec, dtype='float32')


def search_faiss(query: str, topk: int, base: Path):
    if faiss is None:
        raise RuntimeError('faiss-cpu not installed')
    emb_dir = base / 'embeddings'
    index_path = emb_dir / 'faiss.index'
    if not index_path.exists():
        raise RuntimeError('faiss index not found')
    index = faiss.read_index(str(index_path))
    meta_path = emb_dir / 'faiss_meta.jsonl'
    meta = [json.loads(l) for l in meta_path.read_text(encoding='utf-8').splitlines()]
    q = embed_text(query).reshape(1, -1)
    faiss.normalize_L2(q)
    scores, ids = index.search(q, topk)
    results = []
    for score, idx in zip(scores[0], ids[0]):
        if idx < 0:
            continue
        m = meta[idx]
        m['score'] = float(score)
        results.append(m)
    return results


def search_sqlite(query: str, topk: int, base: Path):
    # naive cosine on JSON vectors (fallback)
    q = embed_text(query)
    db = sqlite3.connect(base / 'embeddings' / 'tasks.sqlite')
    cur = db.cursor()
    rows = cur.execute('SELECT chunk_id, task_id, source_path, section, text, vector FROM task_vectors').fetchall()
    scored = []
    for chunk_id, task_id, source_path, section, text, vector_json in rows:
        v = np.array(json.loads(vector_json), dtype='float32')
        score = float(np.dot(q, v) / (np.linalg.norm(q) * np.linalg.norm(v) + 1e-9))
        scored.append((score, {
            'chunkId': chunk_id,
            'taskId': task_id,
            'sourcePath': source_path,
            'section': section,
            'text': text
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [dict(item[1], score=item[0]) for item in scored[:topk]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--query', required=True)
    ap.add_argument('--topk', type=int, default=5)
    ap.add_argument('--container', default='imac')
    args = ap.parse_args()

    base = container_dir(args.container)
    try:
        results = search_faiss(args.query, args.topk, base)
    except Exception:
        results = search_sqlite(args.query, args.topk, base)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
