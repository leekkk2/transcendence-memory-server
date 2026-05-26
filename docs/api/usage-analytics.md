# Usage Analytics API (v0.17)

The server records one row per authenticated HTTP request into the
`api_request_log` SQLite table (lives in the same `queue.db` as the job
queue and embedding backlog). The four `/admin/usage/*` endpoints expose
aggregated views over that table for dashboards and ad-hoc operations.

All endpoints require the standard `X-API-KEY` / `Authorization: Bearer`
header. Replace `https://your-rag.example.com` with your deployment's
public endpoint.

---

## Schema

`api_request_log` — single request detail, retained for
`TM_USAGE_RETENTION_DAYS` (default 30):

| Column          | Type    | Notes |
|-----------------|---------|-------|
| `id`            | INTEGER | autoincrement |
| `ts`            | INTEGER | unix milliseconds |
| `method`        | TEXT    | HTTP verb |
| `path`          | TEXT    | FastAPI route template (e.g. `/containers/{name}/memories/{id}`) |
| `status`        | INTEGER | HTTP status code |
| `latency_ms`    | INTEGER | server-side processing time |
| `container`     | TEXT    | extracted from query / JSON body when present |
| `api_key_hash`  | TEXT    | `sha256(api_key)[:16]` — never the plaintext |
| `ua`            | TEXT    | first 80 chars of `User-Agent`, optional |
| `bytes_in`      | INTEGER | request body length |
| `bytes_out`     | INTEGER | response `Content-Length` (0 for streaming) |
| `error_type`    | TEXT    | one of `none / auth / timeout / quota / permanent / client` |

`daily_usage_rollup` — permanent per-day per-path aggregate computed by
the background worker once per UTC day.

---

## `GET /admin/usage/summary`

Top-line counters + latency for the requested window.

Query params:

| Param    | Type | Default | Allowed                |
|----------|------|---------|------------------------|
| `window` | str  | `24h`   | `1h`, `24h`, `7d`, `30d` |

Example:

```bash
curl -H "X-API-KEY: $TM_API_KEY" \
  "https://your-rag.example.com/admin/usage/summary?window=24h"
```

```json
{
  "window": "24h",
  "total_calls": 12345,
  "total_errors": 23,
  "error_rate": 0.0019,
  "p50_latency_ms": 42,
  "p95_latency_ms": 320,
  "active_containers": 18,
  "active_api_keys": 3,
  "top_endpoints": [
    {"path": "/search", "calls": 8200, "p95": 280},
    {"path": "/embed", "calls": 1200, "p95": 1800}
  ]
}
```

---

## `GET /admin/usage/endpoints`

Endpoint ranking + cold endpoint detection.

| Param    | Type | Default  | Allowed                |
|----------|------|----------|------------------------|
| `window` | str  | `7d`     | `1h`, `24h`, `7d`, `30d` |
| `sort`   | str  | `calls`  | `calls`, `errors`, `p95` |
| `limit`  | int  | `20`     | `1..200`               |

`cold_endpoints` returns paths called fewer than 10 times in the trailing
30 days — feed straight into the "rarely used" dashboard panel.

```bash
curl -H "X-API-KEY: $TM_API_KEY" \
  "https://your-rag.example.com/admin/usage/endpoints?window=7d&sort=p95&limit=10"
```

```json
{
  "window": "7d",
  "sort": "p95",
  "rows": [
    {
      "path": "/embed",
      "calls": 1200,
      "errors": 4,
      "p50_latency_ms": 900,
      "p95_latency_ms": 1800,
      "distinct_containers": 12,
      "last_called_at": 1716700000000
    }
  ],
  "cold_endpoints": [
    {"path": "/documents/file", "calls": 0, "last_called_at": null}
  ]
}
```

---

## `GET /admin/usage/containers`

Container-dimension fan-out — answers "who is calling the server?".

| Param    | Type | Default | Allowed                |
|----------|------|---------|------------------------|
| `window` | str  | `7d`    | `1h`, `24h`, `7d`, `30d` |
| `sort`   | str  | `calls` | `calls` (only)         |
| `limit`  | int  | `50`    | `1..500`               |

The `search_calls / ingest_calls / embed_calls` fields are heuristic
partitions of `calls` based on the path prefix; the breakdown is meant
for dashboard charts, not for billing.

```json
{
  "window": "7d",
  "rows": [
    {
      "container": "your-project",
      "calls": 4200,
      "search_calls": 3100,
      "ingest_calls": 800,
      "embed_calls": 300,
      "last_active": 1716700000000
    }
  ],
  "idle_containers": []
}
```

---

## `GET /admin/usage/timeseries`

Bucketed time series for one endpoint. Drives the dashboard's latency
chart.

| Param     | Type | Default | Allowed              |
|-----------|------|---------|----------------------|
| `path`    | str  | —       | required             |
| `window`  | str  | `7d`    | `1h`, `24h`, `7d`, `30d` |
| `bucket`  | str  | `1h`    | `5m`, `1h`, `1d`     |

The server promotes `bucket` to `1h` for windows > 7 d, and to `1d`
for windows > 30 d so the response stays bounded in size.

```bash
curl -H "X-API-KEY: $TM_API_KEY" \
  "https://your-rag.example.com/admin/usage/timeseries?path=/search&window=7d&bucket=1h"
```

```json
{
  "path": "/search",
  "window": "7d",
  "bucket": "1h",
  "points": [
    {"ts": 1716000000000, "calls": 320, "errors": 1, "p95": 280},
    {"ts": 1716003600000, "calls": 290, "errors": 0, "p95": 240}
  ]
}
```

---

## `POST /admin/usage/cleanup`

Drops rows older than `retention_days` from `api_request_log`. Safe to
call any time — daily rollups are stored in `daily_usage_rollup` and are
not affected.

```bash
curl -X POST -H "X-API-KEY: $TM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"retention_days": 30}' \
  "https://your-rag.example.com/admin/usage/cleanup"
```

```json
{ "deleted_rows": 152340, "kept_rows": 8200 }
```

---

## Configuration

See `.env.example` for the full list of `TM_USAGE_*` knobs. Defaults are
chosen so that out of the box you get analytics with retention 30 d,
flush every 10 ms / 100 records, and one rollup pass per hour.

To turn the whole subsystem off, set:

```
TM_USAGE_ANALYTICS=0
```

The middleware will not register, no rows will be written, and the four
endpoints will still respond (returning zeros over an empty table).
