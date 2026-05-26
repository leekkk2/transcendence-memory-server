"""Shared batch-ingest core used by the ``tm batch`` command.

Self-contained (httpx-based) implementation. The skill ships its own urllib
version that follows the same protocol; this module is the CLI-side mirror so
the server wheel is not touched.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import httpx

DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_BYTES = 512_000  # 500 KB
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
MIN_BATCH_SIZE = 1


# Redaction patterns — kept aligned with skill ``batch-ingest.py`` so both code
# paths produce identical scrubbed payloads.
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token"
            r"|auth[_-]?token|bearer|password|passwd|credential"
            r"|client[_-]?secret|private[_-]?key|signing[_-]?key"
            r"|x[_-]api[_-]key)"
            r"[\s]*[=:\"\s]+[\s\"]*([A-Za-z0-9_\-/.+]{20,})"
        ),
        r"\1=<REDACTED>",
    ),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "<REDACTED-OPENAI-KEY>"),
    (re.compile(r"gh[psohr]_[A-Za-z0-9]{36,}"), "<REDACTED-GITHUB-TOKEN>"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{35}"), "<REDACTED-GOOGLE-KEY>"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "<REDACTED-AWS-KEY>"),
    (re.compile(r"xox[bpsa]-[A-Za-z0-9\-]{10,}"), "<REDACTED-SLACK-TOKEN>"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), "<REDACTED-TG-TOKEN>"),
    (
        re.compile(
            r"-----BEGIN\s+(RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----"
            r"[\s\S]*?"
            r"-----END\s+(RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----",
            re.MULTILINE,
        ),
        "<REDACTED-PRIVATE-KEY>",
    ),
]


def redact_text(text: str) -> str:
    """Apply the canonical redaction patterns to a text payload."""

    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@dataclass
class BatchResult:
    sent: int = 0
    accepted: int = 0
    skipped: int = 0
    failed: list[str] = field(default_factory=list)


def iter_jsonl(path: Path, *, start_line: int = 0) -> Iterator[tuple[int, dict]]:
    """Yield ``(line_no, obj)`` tuples skipping blank / malformed lines."""

    with open(path, encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            if line_no <= start_line:
                continue
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield line_no, obj


def send_batch(
    client: httpx.Client,
    container: str,
    objects: list[dict],
    *,
    auto_embed: bool = False,
) -> dict:
    """POST a single batch to ``/ingest-memory/objects`` with retry + 413 split."""

    payload = {"container": container, "objects": objects, "auto_embed": auto_embed}
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.post("/ingest-memory/objects", json=payload, timeout=120.0)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise
        if response.status_code == 413:
            return _split_and_retry(client, container, objects, auto_embed=auto_embed)
        if response.status_code == 422:
            raise httpx.HTTPStatusError(
                f"422 validation error: {response.text[:600]}",
                request=response.request,
                response=response,
            )
        if response.status_code >= 400:
            last_exc = httpx.HTTPStatusError(
                f"{response.status_code}: {response.text[:300]}",
                request=response.request,
                response=response,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            raise last_exc
        return response.json()
    raise last_exc or RuntimeError("send_batch exhausted retries with no error")


def _split_and_retry(
    client: httpx.Client,
    container: str,
    objects: list[dict],
    *,
    auto_embed: bool,
) -> dict:
    if len(objects) <= MIN_BATCH_SIZE:
        obj_id = objects[0].get("id", "?") if objects else "?"
        return {"accepted": 0, "skipped_413": [obj_id]}
    mid = len(objects) // 2
    r1 = send_batch(client, container, objects[:mid], auto_embed=auto_embed)
    r2 = send_batch(client, container, objects[mid:], auto_embed=auto_embed)
    accepted = r1.get("accepted", 0) + r2.get("accepted", 0)
    skipped = (r1.get("skipped_413") or []) + (r2.get("skipped_413") or [])
    return {"accepted": accepted, "skipped_413": skipped}


def estimate_payload_bytes(objects: Iterable[dict]) -> int:
    return sum(len(json.dumps(o, ensure_ascii=False).encode("utf-8")) for o in objects) + 100


def progress_file_for(input_file: Path) -> Path:
    return Path(str(input_file) + ".progress")


def failed_log_for(input_file: Path) -> Path:
    return Path(str(input_file) + ".failed.jsonl")
