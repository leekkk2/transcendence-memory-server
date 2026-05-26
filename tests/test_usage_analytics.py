"""Tests for v0.17 usage analytics: schema bootstrap, middleware writes,
admin endpoint contracts."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import auth_headers, load_server, make_workspace


def _drain_batcher(server) -> None:
    """Force the middleware batcher to flush so we can observe rows.

    The batcher writes asynchronously on a 10 ms cadence; without a tiny
    sleep + explicit flush the queue may still hold the row when the
    assertion runs.
    """
    mw = _get_usage_middleware(server)
    if mw is None or mw.batcher is None:
        return
    # Run the flush coroutine on the existing TestClient loop.
    import asyncio

    async def _flush():
        await mw.batcher.flush()

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_flush())
        loop.close()
    except RuntimeError:
        pass


def _get_usage_middleware(server):
    """Walk the Starlette middleware stack and return the UsageMiddleware
    instance. We can't just grab it from ``app.middleware`` — Starlette
    wraps middleware in a stack and the resolved instance is created
    lazily on the first request, so this helper does it via
    ``app.build_middleware_stack``-equivalent traversal.
    """
    # FastAPI/Starlette builds the stack lazily; trigger by issuing a
    # request before calling this. Then the cached stack contains
    # CORSMiddleware → UsageMiddleware → … → app.
    stack = server.app.middleware_stack
    while stack is not None:
        if type(stack).__name__ == "UsageMiddleware":
            return stack
        stack = getattr(stack, "app", None)
    return None


# ---------------------------------------------------------------------------
# Pure-function unit tests (no FastAPI loaded — exercises module surface)
# ---------------------------------------------------------------------------


def test_ensure_schema_creates_tables(tmp_path: Path):
    from scripts import usage_analytics

    db = tmp_path / "queue.db"
    usage_analytics.ensure_schema(db)

    with sqlite3.connect(str(db)) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert "api_request_log" in names
    assert "daily_usage_rollup" in names


def test_hash_api_key_stable_and_truncated():
    from scripts import usage_analytics

    h1 = usage_analytics.hash_api_key("test-rag-key")
    h2 = usage_analytics.hash_api_key("test-rag-key")
    assert h1 == h2
    assert h1 is not None
    assert len(h1) == 16
    assert usage_analytics.hash_api_key(None) is None
    assert usage_analytics.hash_api_key("") is None


def test_classify_error_buckets():
    from scripts import usage_analytics

    assert usage_analytics.classify_error(200) == "none"
    assert usage_analytics.classify_error(401) == "auth"
    assert usage_analytics.classify_error(429) == "quota"
    assert usage_analytics.classify_error(504) == "timeout"
    assert usage_analytics.classify_error(500) == "permanent"
    assert usage_analytics.classify_error(404) == "client"


def test_percentile_handles_empty_and_single():
    from scripts.usage_analytics import _percentile

    assert _percentile([], 95) == 0
    assert _percentile([42], 50) == 42
    assert _percentile([10, 20, 30, 40, 50], 50) == 30


# ---------------------------------------------------------------------------
# Rollup function (calls SQLite directly — no FastAPI in the loop)
# ---------------------------------------------------------------------------


def test_rollup_day_aggregates_correctly(tmp_path: Path):
    from scripts import usage_analytics

    db = tmp_path / "queue.db"
    usage_analytics.ensure_schema(db)

    # Insert a known set of rows on 2026-05-25 (UTC).
    day = "2026-05-25"
    # 2026-05-25T12:00:00 UTC → ms
    import datetime as _dt
    base_ms = int(_dt.datetime(2026, 5, 25, 12, 0, tzinfo=_dt.timezone.utc).timestamp() * 1000)

    rows = [
        (base_ms, "GET", "/search", 200, 50, "c1", "h1", "ua", 100, 200, "none"),
        (base_ms + 1, "GET", "/search", 200, 60, "c1", "h1", "ua", 100, 200, "none"),
        (base_ms + 2, "GET", "/search", 500, 70, "c2", "h2", "ua", 100, 200, "permanent"),
        (base_ms + 3, "GET", "/embed", 200, 1500, "c1", "h1", "ua", 50, 50, "none"),
    ]
    with sqlite3.connect(str(db)) as conn:
        conn.executemany(
            "INSERT INTO api_request_log "
            "(ts, method, path, status, latency_ms, container, api_key_hash, "
            "ua, bytes_in, bytes_out, error_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    result = usage_analytics.rollup_day(db, day=day)
    assert result["day"] == day
    assert result["paths_rolled_up"] == 2

    with sqlite3.connect(str(db)) as conn:
        conn.row_factory = sqlite3.Row
        rolled = {r["path"]: dict(r) for r in conn.execute(
            "SELECT * FROM daily_usage_rollup WHERE day = ?", (day,)
        )}
    assert rolled["/search"]["calls"] == 3
    assert rolled["/search"]["errors"] == 1
    assert rolled["/search"]["distinct_containers"] == 2
    assert rolled["/embed"]["calls"] == 1
    assert rolled["/embed"]["errors"] == 0


# ---------------------------------------------------------------------------
# Pure summary / endpoints / containers / timeseries (no middleware loop)
# ---------------------------------------------------------------------------


def _seed_log(db_path: Path, rows: list[tuple]) -> None:
    from scripts import usage_analytics
    usage_analytics.ensure_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO api_request_log "
            "(ts, method, path, status, latency_ms, container, api_key_hash, "
            "ua, bytes_in, bytes_out, error_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


def test_summary_returns_zeros_on_empty_table(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    out = usage_analytics.summary(db, window="24h")
    assert out["total_calls"] == 0
    assert out["total_errors"] == 0
    assert out["error_rate"] == 0.0
    assert out["top_endpoints"] == []


def test_summary_aggregates_recent_rows(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    now_ms = int(time.time() * 1000)
    rows = [
        (now_ms - 1000, "POST", "/search", 200, 50, "alpha", "h1", "ua", 10, 20, "none"),
        (now_ms - 2000, "POST", "/search", 500, 90, "alpha", "h2", "ua", 10, 20, "permanent"),
        (now_ms - 3000, "POST", "/embed", 200, 800, "beta", "h1", "ua", 10, 20, "none"),
    ]
    _seed_log(db, rows)
    out = usage_analytics.summary(db, window="24h")
    assert out["total_calls"] == 3
    assert out["total_errors"] == 1
    assert out["active_containers"] == 2
    assert out["active_api_keys"] == 2
    paths = [t["path"] for t in out["top_endpoints"]]
    assert "/search" in paths
    assert "/embed" in paths


def test_summary_rejects_invalid_window(tmp_path: Path):
    from scripts import usage_analytics
    from fastapi import HTTPException
    db = tmp_path / "queue.db"
    with pytest.raises(HTTPException) as exc:
        usage_analytics.summary(db, window="bogus")
    assert exc.value.status_code == 400


def test_endpoints_sorting_and_cold(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    now_ms = int(time.time() * 1000)
    # /search hot, /embed cold (only 2 calls < 10).
    rows = []
    for i in range(20):
        rows.append((now_ms - i * 100, "POST", "/search", 200, 50, "c1", "h1", "ua", 10, 10, "none"))
    for i in range(2):
        rows.append((now_ms - i * 100, "POST", "/embed", 200, 1500, "c1", "h1", "ua", 10, 10, "none"))
    _seed_log(db, rows)
    out = usage_analytics.endpoints(db, window="7d", sort="calls", limit=10)
    assert out["rows"][0]["path"] == "/search"
    cold_paths = [c["path"] for c in out["cold_endpoints"]]
    assert "/embed" in cold_paths


def test_endpoints_rejects_invalid_sort(tmp_path: Path):
    from scripts import usage_analytics
    from fastapi import HTTPException
    db = tmp_path / "queue.db"
    with pytest.raises(HTTPException):
        usage_analytics.endpoints(db, sort="bogus")
    with pytest.raises(HTTPException):
        usage_analytics.endpoints(db, limit=0)


def test_containers_groups_by_container(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    now_ms = int(time.time() * 1000)
    rows = [
        (now_ms - 1000, "POST", "/search", 200, 50, "alpha", "h1", "ua", 10, 10, "none"),
        (now_ms - 2000, "POST", "/search", 200, 60, "alpha", "h1", "ua", 10, 10, "none"),
        (now_ms - 3000, "POST", "/embed", 200, 80, "alpha", "h1", "ua", 10, 10, "none"),
        (now_ms - 4000, "POST", "/search", 200, 70, "beta", "h2", "ua", 10, 10, "none"),
    ]
    _seed_log(db, rows)
    out = usage_analytics.containers(db, window="7d", limit=10)
    by_name = {r["container"]: r for r in out["rows"]}
    assert by_name["alpha"]["calls"] == 3
    assert by_name["alpha"]["search_calls"] == 2
    assert by_name["alpha"]["embed_calls"] == 1
    assert by_name["beta"]["calls"] == 1


def test_timeseries_buckets_correctly(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    now_ms = int(time.time() * 1000)
    # 5 rows within the same 1 h bucket, then 1 row 2 h earlier.
    bucket_ms = 3600 * 1000
    bucket_a = (now_ms // bucket_ms) * bucket_ms
    bucket_b = bucket_a - bucket_ms * 2
    rows = []
    for i in range(5):
        rows.append((bucket_a + i, "POST", "/search", 200, 50, "c1", "h1", "ua", 10, 10, "none"))
    rows.append((bucket_b + 10, "POST", "/search", 500, 999, "c1", "h1", "ua", 10, 10, "permanent"))
    _seed_log(db, rows)
    out = usage_analytics.timeseries(db, path="/search", window="24h", bucket="1h")
    assert out["bucket"] == "1h"
    assert len(out["points"]) >= 2
    by_ts = {p["ts"]: p for p in out["points"]}
    assert by_ts[bucket_a]["calls"] == 5
    assert by_ts[bucket_b]["calls"] == 1
    assert by_ts[bucket_b]["errors"] == 1


def test_timeseries_promotes_bucket_for_long_window(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    _seed_log(db, [])
    # 30d window with 5m bucket → should be promoted to 1h.
    out = usage_analytics.timeseries(db, path="/search", window="30d", bucket="5m")
    assert out["bucket"] == "1h"


def test_cleanup_deletes_old_rows(tmp_path: Path):
    from scripts import usage_analytics
    db = tmp_path / "queue.db"
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 40 * 86_400 * 1000  # 40 days ago
    rows = [
        (now_ms, "POST", "/search", 200, 50, "c1", "h1", "ua", 10, 10, "none"),
        (old_ms, "POST", "/search", 200, 50, "c1", "h1", "ua", 10, 10, "none"),
    ]
    _seed_log(db, rows)
    out = usage_analytics.cleanup(db, retention_days=30)
    assert out["deleted_rows"] == 1
    assert out["kept_rows"] == 1


# ---------------------------------------------------------------------------
# End-to-end middleware tests — go through the FastAPI app + TestClient
# ---------------------------------------------------------------------------


def test_middleware_writes_row_on_request(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # Hit a known authenticated endpoint and force the batcher to flush.
    resp = client.get("/containers", headers=auth_headers())
    assert resp.status_code == 200
    _drain_batcher(server)

    db_path = workspace / "tasks" / "rag" / "queue.db"
    assert db_path.exists()
    with sqlite3.connect(str(db_path)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM api_request_log WHERE path = ?", ("/containers",)
        ).fetchone()[0]
    assert cnt >= 1


def test_middleware_disabled_via_env(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(
        workspace, monkeypatch, extra_env={"TM_USAGE_ANALYTICS": "0"}
    )
    client = TestClient(server.app)

    client.get("/containers", headers=auth_headers())
    # When disabled the middleware is not registered → no schema, no rows.
    mw = _get_usage_middleware(server)
    assert mw is None


def test_middleware_skips_health_path(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    client.get("/health")
    _drain_batcher(server)
    db_path = workspace / "tasks" / "rag" / "queue.db"
    if not db_path.exists():
        return  # schema not bootstrapped because /health was ignored
    with sqlite3.connect(str(db_path)) as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM api_request_log WHERE path = '/health'"
        ).fetchone()[0]
    assert cnt == 0


# ---------------------------------------------------------------------------
# Admin endpoint contract tests
# ---------------------------------------------------------------------------


def test_admin_summary_endpoint_contract(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    # Drive some traffic so the batcher has something to flush.
    client.get("/containers", headers=auth_headers())
    client.get("/containers", headers=auth_headers())
    _drain_batcher(server)

    resp = client.get("/admin/usage/summary?window=24h", headers=auth_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "24h"
    assert "total_calls" in body
    assert "p50_latency_ms" in body
    assert isinstance(body["top_endpoints"], list)


def test_admin_summary_requires_auth(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get("/admin/usage/summary?window=24h")
    assert resp.status_code == 401


def test_admin_endpoints_contract(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.get(
        "/admin/usage/endpoints?window=7d&sort=calls&limit=5",
        headers=auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sort"] == "calls"
    assert isinstance(body["rows"], list)
    assert isinstance(body["cold_endpoints"], list)


def test_admin_endpoints_rejects_bad_sort(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get(
        "/admin/usage/endpoints?sort=oops", headers=auth_headers()
    )
    assert resp.status_code == 400


def test_admin_containers_contract(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get(
        "/admin/usage/containers?window=7d&limit=10", headers=auth_headers()
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["window"] == "7d"
    assert "rows" in body
    assert "idle_containers" in body


def test_admin_timeseries_contract(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get(
        "/admin/usage/timeseries?path=/search&window=7d&bucket=1h",
        headers=auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "/search"
    assert body["bucket"] == "1h"
    assert isinstance(body["points"], list)


def test_admin_timeseries_requires_path(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)
    resp = client.get(
        "/admin/usage/timeseries?window=7d&bucket=1h", headers=auth_headers()
    )
    # FastAPI returns 422 for missing required query param.
    assert resp.status_code in (400, 422)


def test_admin_cleanup_contract(tmp_path: Path, monkeypatch):
    workspace = make_workspace(tmp_path)
    server = load_server(workspace, monkeypatch)
    client = TestClient(server.app)

    resp = client.post(
        "/admin/usage/cleanup",
        headers=auth_headers(),
        json={"retention_days": 30},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "deleted_rows" in body
    assert "kept_rows" in body
