#!/usr/bin/env python3
"""Background CLI for RAG knowledge-graph ingestion.

Spawned as a child process by the job worker for ``ingest-document-text`` and
``ingest-document-file`` jobs. Building the knowledge graph (LightRAG /
RAG-Anything) routinely takes tens of seconds to minutes; running it here —
off the request thread — lets ``POST /documents/text`` and ``/documents/upload``
return immediately instead of blocking until the graph finishes.

Exit code 0 on success, non-zero on failure (error printed to stderr so the
worker captures it in the job's ``last_error``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

try:
    from rag_engine import get_lightrag
except ModuleNotFoundError:  # pragma: no cover - package import path
    from scripts.rag_engine import get_lightrag


_logger = logging.getLogger("task_rag_graph_ingest")
if not _logger.handlers and not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _parsed_dir(input_path: Path) -> Path:
    """RAG-Anything parser 中间产物目录（可能数百 MB），与输入文件同级。"""
    return input_path.parent / f"{input_path.stem}_parsed"


def _cleanup_parsed(input_path: Path) -> None:
    parsed = _parsed_dir(input_path)
    if parsed.is_dir():
        shutil.rmtree(parsed, ignore_errors=True)


async def _ingest_text(container: str, input_path: Path) -> dict:
    """text 模式：读 inbox 文件正文 → LightRAG 建图。"""
    text = input_path.read_text(encoding="utf-8", errors="ignore")
    if not text.strip():
        raise ValueError(f"input file is empty: {input_path}")
    lightrag = await get_lightrag(container)
    await lightrag.ainsert(text)
    return {"mode": "text", "chars": len(text)}


async def _ingest_file(container: str, input_path: Path, parse_method: str | None) -> dict:
    """file 模式：RAG-Anything 解析多模态文档 → 写入同一知识图谱。"""
    try:
        from raganything_engine import get_raganything
    except ModuleNotFoundError:  # pragma: no cover - package import path
        from scripts.raganything_engine import get_raganything

    rag = await get_raganything(container)
    parse_output = _parsed_dir(input_path)
    parse_output.mkdir(parents=True, exist_ok=True)
    parser_kwargs: dict = {}
    backend = os.environ.get("RAG_PARSER_BACKEND")
    if backend:
        parser_kwargs["backend"] = backend
    lang = os.environ.get("RAG_PARSER_LANG")
    if lang:
        parser_kwargs["lang"] = lang
    await rag.process_document_complete(
        file_path=str(input_path),
        output_dir=str(parse_output),
        parse_method=parse_method or os.environ.get("RAG_PARSE_METHOD", "auto"),
        **parser_kwargs,
    )
    return {"mode": "file", "filename": input_path.name}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG knowledge-graph ingest worker CLI")
    parser.add_argument("--container", default="default")
    parser.add_argument("--mode", choices=("text", "file"), required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--description", default="")
    parser.add_argument("--parse-method", default="")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_file():
        print(f"input file not found: {input_path}", file=sys.stderr)
        return 2

    input_bytes = input_path.stat().st_size
    try:
        if args.mode == "text":
            summary = asyncio.run(_ingest_text(args.container, input_path))
        else:
            summary = asyncio.run(
                _ingest_file(args.container, input_path, args.parse_method or None)
            )
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit non-zero
        _logger.error("graph ingest failed: %s", exc, exc_info=True)
        print(f"graph ingest failed: {exc}", file=sys.stderr)
        # 失败时保留输入文件，让队列重试能再次读取；只清理可能很大的解析中间产物。
        _cleanup_parsed(input_path)
        return 1

    # 成功后删除 inbox 输入文件与解析产物，避免 _inbox 堆积。
    input_path.unlink(missing_ok=True)
    _cleanup_parsed(input_path)
    print(json.dumps(
        {"code": 0, "container": args.container, "input_bytes": input_bytes, **summary},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
