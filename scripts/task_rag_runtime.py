#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import requests


WS = Path(os.environ.get('WORKSPACE', Path(__file__).resolve().parents[1]))
TASKS = WS / 'tasks'

API_KEY = os.getenv('EMBEDDING_API_KEY', '')
MODEL = os.getenv('EMBEDDING_MODEL', 'gemini-embedding-001')
EMBEDDINGS_BASE_URL = (
    os.getenv('EMBEDDING_BASE_URL')
    or os.getenv('EMBEDDINGS_BASE_URL')
    or 'https://newapi.zweiteng.tk/v1'
)
GOOGLE_BASE_URL = os.getenv('GOOGLE_EMBEDDING_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/models')


def container_dir(container: str) -> Path:
    base = TASKS / 'rag' / 'containers' / container
    base.mkdir(parents=True, exist_ok=True)
    return base


def lancedb_dir(container: str) -> Path:
    base = container_dir(container) / 'lancedb'
    base.mkdir(parents=True, exist_ok=True)
    return base


def embed_text(text: str) -> np.ndarray:
    if not API_KEY:
        raise RuntimeError('EMBEDDING_API_KEY not set')

    last_err = None
    url = f'{EMBEDDINGS_BASE_URL.rstrip("/")}/embeddings'
    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json',
    }
    payload = {'model': MODEL, 'input': text}
    for attempt in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=90)
            response.raise_for_status()
            data = response.json()
            return np.array(data['data'][0]['embedding'], dtype='float32')
        except Exception as exc:  # pragma: no cover - provider failures vary
            last_err = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))

    if API_KEY.startswith('AIza'):
        google_url = f'{GOOGLE_BASE_URL.rstrip("/")}/{MODEL}:embedContent?key={API_KEY}'
        google_payload = {'content': {'parts': [{'text': text}]}}
        response = requests.post(google_url, json=google_payload, timeout=90)
        response.raise_for_status()
        data = response.json()
        return np.array(data['embedding']['values'], dtype='float32')

    raise RuntimeError(f'Embedding request failed after retries: {last_err}')
