"""Tests for scripts/eval_retrieval.py metric functions.

确保 recall@k / precision@k / nDCG@k / MRR 实现符合标准 IR 教科书定义。
"""
from __future__ import annotations
import importlib
import sys
from pathlib import Path
import pytest


@pytest.fixture()
def m(monkeypatch):
    scripts_dir = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts_dir))
    if "eval_retrieval" in sys.modules:
        del sys.modules["eval_retrieval"]
    return importlib.import_module("eval_retrieval")


# ---------------- recall ----------------

def test_recall_perfect(m):
    """top-5 完全覆盖 ideal → recall=1.0"""
    assert m.recall_at_k(["a", "b", "c", "d", "e"], ["a", "b", "c"], 5) == 1.0


def test_recall_partial(m):
    """ideal 3 个，top-5 只命中 2 → recall = 2/3"""
    assert m.recall_at_k(["a", "b", "x", "y", "z"], ["a", "b", "c"], 5) == pytest.approx(2 / 3)


def test_recall_no_ideal(m):
    """没标 ideal → 任何结果都算 OK (1.0)，让 draft 不影响 mean"""
    assert m.recall_at_k(["a", "b"], [], 5) == 1.0


def test_recall_no_hits(m):
    """top-5 完全没命中 → 0"""
    assert m.recall_at_k(["x", "y", "z"], ["a", "b"], 5) == 0.0


# ---------------- precision ----------------

def test_precision_perfect(m):
    """top-5 全是 ideal 项 → precision = 5/5 = 1.0"""
    assert m.precision_at_k(["a", "b", "c", "d", "e"], ["a", "b", "c", "d", "e"], 5) == 1.0


def test_precision_partial(m):
    """top-5 命中 2 → precision = 2/5"""
    assert m.precision_at_k(["a", "b", "x", "y", "z"], ["a", "b", "c"], 5) == pytest.approx(2 / 5)


def test_precision_no_ideal(m):
    """没 ideal → 1.0（避免 div by zero + 让 draft 不算）"""
    assert m.precision_at_k(["a", "b"], [], 5) == 1.0


# ---------------- nDCG ----------------

def test_ndcg_perfect_order(m):
    """top-5 完全是 ideal 且按 ideal 顺序排 → nDCG = 1.0"""
    assert m.ndcg_at_k(["a", "b", "c"], ["a", "b", "c"], 5) == pytest.approx(1.0)


def test_ndcg_no_hits(m):
    """top-5 完全没命中 ideal → DCG=0 → nDCG=0"""
    assert m.ndcg_at_k(["x", "y"], ["a", "b"], 5) == 0.0


def test_ndcg_no_ideal(m):
    """没 ideal → 1.0"""
    assert m.ndcg_at_k(["a", "b"], [], 5) == 1.0


def test_ndcg_partial_late(m):
    """理想 1 个，命中但在 rank 2 → nDCG < 1"""
    # rel = [0, 1, 0, 0, 0]; DCG = 1/log2(3) ≈ 0.63
    # ideal_rel = [1]; ideal DCG = 1/log2(2) = 1
    result = m.ndcg_at_k(["x", "a", "y", "z", "w"], ["a"], 5)
    assert 0 < result < 1
    import math
    expected = (2**1 - 1) / math.log2(3)
    assert result == pytest.approx(expected)


# ---------------- MRR ----------------

def test_mrr_top1(m):
    """top-1 即命中 → MRR=1"""
    assert m.mrr(["a", "b", "c"], ["a"]) == 1.0


def test_mrr_top3(m):
    """rank 3 命中 → MRR = 1/3"""
    assert m.mrr(["x", "y", "a", "z"], ["a"]) == pytest.approx(1 / 3)


def test_mrr_no_hit(m):
    """完全没命中 → 0"""
    assert m.mrr(["x", "y"], ["a"]) == 0.0


def test_mrr_no_ideal(m):
    """没 ideal → 1.0"""
    assert m.mrr(["x"], []) == 1.0


def test_mrr_first_match_wins(m):
    """ideal 多个时取首个命中的 rank"""
    # rank 2 命中 b（在 ideal 内）→ 1/2
    assert m.mrr(["x", "b", "a"], ["a", "b", "c"]) == pytest.approx(1 / 2)
