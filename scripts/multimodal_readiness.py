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
import sys
from raganything.parser import get_parser
parser = get_parser(sys.argv[1])
raise SystemExit(0 if parser.check_installation() else 1)
"""


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def parser_readiness() -> tuple[bool, str]:
    parser = os.environ.get('RAG_PARSER', 'mineru')
    key = (parser, sys.executable, os.environ.get('PATH', ''))
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            ready = cached[1]
        else:
            try:
                result = subprocess.run(
                    [sys.executable, '-c', _PROBE, parser],
                    capture_output=True, text=True, timeout=15,
                )
                ready = result.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                ready = False
            _cache[key] = (time.monotonic(), ready)
    return ready, '' if ready else 'multimodal parser unavailable'
