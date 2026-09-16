"""Bounded parser probe, isolated from the API process and cached briefly."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

_cache: dict[tuple[str, str, str], tuple[float, bool]] = {}
_lock = threading.Lock()
_PROBE = """
import sys, shutil, importlib.util, importlib.metadata
parser, backend = sys.argv[1:3]
importlib.metadata.version('raganything')
if parser == 'mineru':
    importlib.metadata.version('mineru')
    required = ('torch', 'onnxruntime', 'cv2', 'transformers') if backend == 'pipeline' else ()
    ready = bool(shutil.which('mineru')) and all(importlib.util.find_spec(x) is not None for x in required)
elif parser == 'docling':
    ready = importlib.util.find_spec('docling') is not None
else:
    from raganything.parser import get_parser
    ready = get_parser(parser).check_installation()
raise SystemExit(0 if ready else 1)
"""


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def parser_readiness() -> tuple[bool, str]:
    parser = os.environ.get('RAG_PARSER', 'mineru')
    backend = os.environ.get('RAG_PARSER_BACKEND', '')
    key = (parser + ':' + backend, sys.executable, os.environ.get('PATH', ''))
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            ready = cached[1]
        else:
            try:
                result = subprocess.run(
                    [sys.executable, '-c', _PROBE, parser, backend],
                    capture_output=True, text=True, timeout=15,
                )
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ready = False
            _cache[key] = (time.monotonic(), ready)
    return ready, '' if ready else 'multimodal parser unavailable'
