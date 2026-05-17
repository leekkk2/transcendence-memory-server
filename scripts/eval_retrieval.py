#!/usr/bin/env python3
"""Ground-truth retrieval eval — 对 ground-truth-queries.jsonl 每条 query 跑双轨 search，
计算 Recall@5 / Precision@5 / nDCG@5 / MRR，输出 markdown 报告。

用法：
    python scripts/eval_retrieval.py docs/research/ground-truth-queries.jsonl \\
        --main-container my-container --mirror-suffix _openai \\
        --endpoint https://your-tm-server.example.com --api-key sk-... \\
        --topk 5 --output docs/research/retrieval-trend-2026-05.md

环境：
    可用 TM_ENDPOINT / TM_API_KEY env 替代 --endpoint / --api-key

输入 JSONL schema：
    {"id":"q-001","container":"my-container","query":"...","ideal_chunks":["chunkA",...],"status":"draft|active|deprecated"}

跳过 status=draft / deprecated 的 query（draft = 待人工标注；deprecated = 历史 query 不算分）。
"""
from __future__ import annotations
import argparse
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any


def dcg(rel_list: list[int]) -> float:
    return sum((2 ** rel - 1) / math.log2(i + 2) for i, rel in enumerate(rel_list))


def ndcg_at_k(returned_ids: list[str], ideal_ids: list[str], k: int = 5) -> float:
    rel = [1 if cid in ideal_ids else 0 for cid in returned_ids[:k]]
    ideal_rel = [1] * min(len(ideal_ids), k)
    if not ideal_rel:
        return 1.0  # ground-truth 没标 ideal → 任何结果都算 OK
    if not rel:
        return 0.0
    return dcg(rel) / dcg(ideal_rel)


def recall_at_k(returned_ids: list[str], ideal_ids: list[str], k: int = 5) -> float:
    if not ideal_ids:
        return 1.0
    hits = set(returned_ids[:k]) & set(ideal_ids)
    return len(hits) / len(ideal_ids)


def precision_at_k(returned_ids: list[str], ideal_ids: list[str], k: int = 5) -> float:
    if not ideal_ids:
        return 1.0
    hits = set(returned_ids[:k]) & set(ideal_ids)
    return len(hits) / k


def mrr(returned_ids: list[str], ideal_ids: list[str]) -> float:
    """MRR@k where k = len(returned_ids)（本工具默认 topk=5 → 实际是 MRR@5）。

    与教科书 full MRR 区别：本工具不拉全表，rank 只在已返回的 top-k 里搜。
    返回 1/rank（首个命中），无命中返回 0；没标 ideal 返回 1.0（draft 不影响 mean）。
    """
    if not ideal_ids:
        return 1.0
    ideal_set = set(ideal_ids)
    for i, cid in enumerate(returned_ids):
        if cid in ideal_set:
            return 1.0 / (i + 1)
    return 0.0


def search(endpoint: str, api_key: str, container: str, query: str, topk: int = 5) -> dict[str, Any]:
    body = json.dumps({"container": container, "query": query, "topk": topk}).encode()
    req = urllib.request.Request(
        f"{endpoint}/search",
        data=body,
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            elapsed = (time.perf_counter() - t0) * 1000
            data = json.loads(r.read())
            results = data.get("results", [])
            return {
                "ok": data.get("status") == "ok",
                "ms": elapsed,
                "chunk_ids": [r.get("chunkId", "") for r in results],
            }
    except Exception as e:  # 含 HTTPError / URLError / 解析失败等
        return {"ok": False, "ms": (time.perf_counter() - t0) * 1000, "err": str(e)[:120], "chunk_ids": []}


def evaluate(queries: list[dict], endpoint: str, api_key: str,
             mirror_suffix: str, topk: int) -> tuple[list[dict], dict]:
    rows = []
    for q in queries:
        if q.get("status", "active") in ("draft", "deprecated"):
            continue
        # 必填字段防御 — 缺 container/query 直接跳过 + 提示
        if "container" not in q or "query" not in q:
            print(f"WARN: skip {q.get('id', '?')} — missing required field 'container' or 'query'",
                  file=sys.stderr)
            continue
        ideal = q.get("ideal_chunks", [])
        main_container = q["container"]
        mirror_container = f"{main_container}{mirror_suffix}"

        main = search(endpoint, api_key, main_container, q["query"], topk)
        mirror = search(endpoint, api_key, mirror_container, q["query"], topk)

        main_metrics = {
            "recall@5": recall_at_k(main["chunk_ids"], ideal, topk) if main["ok"] else 0.0,
            "precision@5": precision_at_k(main["chunk_ids"], ideal, topk) if main["ok"] else 0.0,
            "ndcg@5": ndcg_at_k(main["chunk_ids"], ideal, topk) if main["ok"] else 0.0,
            "mrr": mrr(main["chunk_ids"], ideal) if main["ok"] else 0.0,
            "latency_ms": main["ms"],
        }
        mirror_metrics = {
            "recall@5": recall_at_k(mirror["chunk_ids"], ideal, topk) if mirror["ok"] else 0.0,
            "precision@5": precision_at_k(mirror["chunk_ids"], ideal, topk) if mirror["ok"] else 0.0,
            "ndcg@5": ndcg_at_k(mirror["chunk_ids"], ideal, topk) if mirror["ok"] else 0.0,
            "mrr": mrr(mirror["chunk_ids"], ideal) if mirror["ok"] else 0.0,
            "latency_ms": mirror["ms"],
        }
        rows.append({
            "id": q["id"],
            "query": q["query"],
            "ideal_n": len(ideal),
            "main": {"container": main_container, **main_metrics, "ok": main["ok"]},
            "mirror": {"container": mirror_container, **mirror_metrics, "ok": mirror["ok"]},
        })

    if not rows:
        return rows, {}

    def avg(metric: str, side: str) -> float:
        vals = [r[side][metric] for r in rows if r[side]["ok"]]
        return sum(vals) / len(vals) if vals else 0.0

    main_ok = sum(1 for r in rows if r["main"]["ok"])
    mirror_ok = sum(1 for r in rows if r["mirror"]["ok"])

    summary = {
        "n_queries": len(rows),
        "main_ok": main_ok,
        "mirror_ok": mirror_ok,
        "main_recall@5": avg("recall@5", "main"),
        "main_precision@5": avg("precision@5", "main"),
        "main_ndcg@5": avg("ndcg@5", "main"),
        "main_mrr": avg("mrr", "main"),
        "main_latency_ms_avg": avg("latency_ms", "main"),
        "mirror_recall@5": avg("recall@5", "mirror"),
        "mirror_precision@5": avg("precision@5", "mirror"),
        "mirror_ndcg@5": avg("ndcg@5", "mirror"),
        "mirror_mrr": avg("mrr", "mirror"),
        "mirror_latency_ms_avg": avg("latency_ms", "mirror"),
    }
    return rows, summary


def render_markdown(rows: list[dict], summary: dict, args) -> str:
    out = []
    out.append(f"# Retrieval eval — {time.strftime('%Y-%m-%d %H:%M', time.localtime())}\n")
    out.append(f"- Ground-truth: `{args.input}`")
    out.append(f"- Endpoint: `{args.endpoint}`")
    out.append(f"- Main vs mirror suffix: `{args.mirror_suffix}`")
    out.append(f"- topk: {args.topk}")
    out.append(f"- Queries: {summary.get('n_queries', 0)} (skipped draft/deprecated)")
    out.append(f"- Main side OK: {summary.get('main_ok', 0)}/{summary.get('n_queries', 0)}  ·  "
               f"Mirror side OK: {summary.get('mirror_ok', 0)}/{summary.get('n_queries', 0)}\n")

    if not rows:
        out.append("⚠ No queries with ground-truth (all draft/deprecated). Annotate `ideal_chunks` first.")
        return "\n".join(out)

    out.append("## Summary\n")
    out.append("| metric | main | mirror | delta |")
    out.append("|---|---|---|---|")
    for m in ["recall@5", "precision@5", "ndcg@5", "mrr"]:
        mm = summary[f"main_{m}"]
        mr = summary[f"mirror_{m}"]
        out.append(f"| {m} | {mm:.3f} | {mr:.3f} | {mr - mm:+.3f} |")
    out.append(f"| latency_ms_avg | {summary['main_latency_ms_avg']:.0f} | "
               f"{summary['mirror_latency_ms_avg']:.0f} | "
               f"{summary['mirror_latency_ms_avg'] - summary['main_latency_ms_avg']:+.0f} |")

    out.append("\n## Per-query\n")
    out.append("| id | query | ideal_n | main R@5 / nDCG@5 / lat | mirror R@5 / nDCG@5 / lat |")
    out.append("|---|---|---|---|---|")
    for r in rows:
        # markdown 表格 cell 必须转义 | 和换行，否则表格结构会被破坏
        q_safe = r['query'].replace("|", "\\|").replace("\n", " ")[:50]
        out.append(
            f"| {r['id']} | {q_safe} | {r['ideal_n']} | "
            f"{r['main']['recall@5']:.2f} / {r['main']['ndcg@5']:.2f} / {r['main']['latency_ms']:.0f} ms | "
            f"{r['mirror']['recall@5']:.2f} / {r['mirror']['ndcg@5']:.2f} / {r['mirror']['latency_ms']:.0f} ms |"
        )
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("input", help="Path to ground-truth-queries.jsonl")
    p.add_argument("--endpoint", default=os.environ.get("TM_ENDPOINT", "https://your-tm-server.example.com"))
    p.add_argument("--api-key", default=os.environ.get("TM_API_KEY", ""))
    p.add_argument("--mirror-suffix", default="_openai", help="Suffix to append to container name for mirror track")
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--output", default=None, help="Write markdown to this path; stdout if not set")
    args = p.parse_args()

    if not args.api_key:
        print("ERR: --api-key required (or set TM_API_KEY env)", file=sys.stderr)
        return 1
    # 安全提示：CLI 传 key 会进 ps argv 暴露给同主机其他用户
    if not os.environ.get("TM_API_KEY") and "--api-key" in sys.argv:
        print("WARN: --api-key on CLI exposes the key in `ps` argv; prefer TM_API_KEY env var", file=sys.stderr)

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"ERR: input not found: {in_path}", file=sys.stderr)
        return 1

    queries = []
    with in_path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                queries.append(json.loads(line))
            except json.JSONDecodeError as e:
                # 一条坏 JSONL 行不应整脚本崩；跳过 + 继续
                print(f"WARN: skip line {lineno}: invalid JSON ({e})", file=sys.stderr)

    rows, summary = evaluate(queries, args.endpoint, args.api_key, args.mirror_suffix, args.topk)
    md = render_markdown(rows, summary, args)

    if args.output:
        # atomic write — 防中途崩溃留下半写报告
        out_path = Path(args.output)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp_path.write_text(md)
        tmp_path.replace(out_path)
        print(f"wrote {out_path} ({len(md)} bytes)")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
