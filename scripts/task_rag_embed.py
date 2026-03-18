#!/usr/bin/env python3
"""Embed manifest.jsonl using gemini-embedding-001 and build FAISS + SQLite stores.

Requires:
- requests
- numpy
- faiss-cpu (for FAISS index)

Env:
- EMBEDDING_BASE_URL (default: https://generativelanguage.googleapis.com/v1beta/models)
- EMBEDDING_API_KEY
- EMBEDDING_MODEL (default: gemini-embedding-001)

Usage:
  task_rag_embed.py
"""
from __future__ import annotations
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
# Prefer OpenAI-compatible embeddings endpoint (your newapi gateway), fallback to Google GenAI API.
EMBEDDINGS_BASE_URL = os.getenv('EMBEDDINGS_BASE_URL', 'https://newapi.zweiteng.tk/v1')
GOOGLE_BASE_URL = os.getenv('GOOGLE_EMBEDDING_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/models')


def embed_text(text: str):
    if not API_KEY:
        raise RuntimeError('EMBEDDING_API_KEY not set')

    # 1) OpenAI-compatible /embeddings
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
        # 2) Google GenAI embedContent
        url = f"{GOOGLE_BASE_URL.rstrip('/')}/{MODEL}:embedContent?key={API_KEY}"
        payload = {"content": {"parts": [{"text": text}]}}
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        vec = data['embedding']['values']
        return np.array(vec, dtype='float32')


def build_sqlite(chunks, vectors, emb_dir):
    db = emb_dir / 'tasks.sqlite'
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS task_vectors (
        chunk_id TEXT PRIMARY KEY,
        task_id TEXT,
        source_path TEXT,
        section TEXT,
        text TEXT,
        vector TEXT
    )''')
    cur.execute('DELETE FROM task_vectors')
    for meta, vec in zip(chunks, vectors):
        cur.execute(
            'INSERT OR REPLACE INTO task_vectors VALUES (?, ?, ?, ?, ?, ?)',
            (meta['chunkId'], meta['taskId'], meta['sourcePath'], meta['section'], meta['text'], json.dumps(vec.tolist()))
        )
    conn.commit()
    conn.close()


def build_faiss(chunks, vectors, emb_dir):
    if faiss is None:
        raise RuntimeError('faiss-cpu not installed')
    mat = np.vstack(vectors)
    index = faiss.IndexFlatIP(mat.shape[1])
    faiss.normalize_L2(mat)
    index.add(mat)
    faiss.write_index(index, str(emb_dir / 'faiss.index'))

    # store metadata
    meta_path = emb_dir / 'faiss_meta.jsonl'
    with meta_path.open('w', encoding='utf-8') as f:
        for meta in chunks:
            f.write(json.dumps(meta, ensure_ascii=False) + '\n')


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--container', default='imac')
    args = ap.parse_args()

    base = container_dir(args.container)
    manifest = base / 'manifest.jsonl'
    emb_dir = base / 'embeddings'
    emb_dir.mkdir(parents=True, exist_ok=True)

    chunks = []
    vectors = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        meta = json.loads(line)
        text = meta.get('text','')
        if not text:
            continue
        vec = embed_text(text)
        chunks.append(meta)
        vectors.append(vec)

    if not chunks:
        print('no chunks')
        return

    build_sqlite(chunks, vectors, emb_dir)
    build_faiss(chunks, vectors, emb_dir)
    print('embedded', len(chunks))


if __name__ == '__main__':
    main()
