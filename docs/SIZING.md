# Sizing & tuning

How to pick `mem_limit` / `cpus` / protection thresholds for the host you're
deploying on, and how to diagnose 503s when the gate trips.

## TL;DR — preset table

| Host RAM | Host cores | `mem_limit` | `memswap_limit` | `cpus` | `TM_MIN_AVAILABLE_MEM_MB` | `TM_MAX_LOAD_PER_CPU` | `TM_MAX_CONCURRENT_INGESTS` |
|---|---|---|---|---|---|---|---|
| ≤ 4 GB | 1-2 | `1500m` | `1500m` | `1.0` | `400` | `4.0` | `1` |
| 8 GB | 2 | `3g` *(base)* | `3g` | `1.5` *(base)* | `800` *(default)* | `4.0` | `1` |
| 16 GB | 4 | `6g` | `6g` | `2.0` | `1000` | `4.0` | `1` |
| 32 GB+ | 8+ | `12g` | `12g` | `3.0` | `1500` | `4.0` | `2` |

Apply via `docker-compose.override.yml`. Template:
[`docker-compose.override.example.yml`](../docker-compose.override.example.yml).

## Why the table looks like this

Three rules govern the picks.

### 1. `memswap_limit == mem_limit` (swap disabled inside container)

Letting the container swap turns transient memory pressure into multi-second
latency spikes (LanceDB / embeddings hit anonymous pages constantly). Disable
container swap by setting `memswap_limit` equal to `mem_limit`. Host swap is
unaffected.

### 2. `TM_MIN_AVAILABLE_MEM_MB` must scale with `mem_limit`

The gate compares against **in-container available memory** (cgroup-relative),
NOT host available. This matters because:

- A 6 GB container on a 23 GB host: cgroup sees 6 GB cap, current usage 2 GB,
  in-container available = 4 GB
- A 1.5 GB container on the same host: cgroup sees 1.5 GB cap, current usage
  0.8 GB, in-container available = 700 MB

If you raise `mem_limit` 4× but leave `TM_MIN_AVAILABLE_MEM_MB` at the 800
default, the threshold becomes meaningless (always passes). If you shrink
`mem_limit` to 1500m but leave the threshold at 800, normal steady-state usage
trips 503 even though the host is idle.

Rule of thumb: **threshold ≈ 15-20% of `mem_limit`**, with a floor of 400 MB
(below that LanceDB embedding loads will OOM before the gate trips).

### 3. `TM_MAX_LOAD_PER_CPU` watches the host, not the container

`load_1min` comes from `/proc/loadavg` (host-wide). The denominator is the
host's `os.cpu_count()`, not the container's cgroup CPU quota. So bumping
`cpus: 1.5 → 3.0` does NOT relax this threshold — it lets the container
finish work faster, which lowers measured load organically. Keep the threshold
at `4.0` unless you co-locate noisy neighbors.

## Trap: copying `1500m` onto a big host

The pre-2026-05-15 `docker-compose.override.example.yml` shipped
`mem_limit: 1500m` as a generic example. Operators on 16-32 GB dedicated
hosts who copied it verbatim got two failures:

1. RAM wasted — 14+ GB of host available, 700 MB of in-container available
2. Spurious 503s on `/embed` and `/query` once container usage reached
   ~700 MB, despite the host being idle

Symptom in `/health`:

```json
{
  "warnings": ["system pressure: container memory pressure: available=747MB < threshold 800MB"],
  "system": {"cgroup_mem_limit_mb": 1500, "cgroup_mem_available_mb": 747, ...},
  "thresholds": {"min_available_mem_mb": 800, "max_load_per_cpu": 4.0, ...},
  "accepting_ingest": false
}
```

Fix: bump `mem_limit` to the table value, restart container, no code change.

## Symptom → fix mapping

| What you see | Likely cause | Fix |
|---|---|---|
| HTTP 503 `container memory pressure: available=X < threshold Y` | `mem_limit` too tight for steady-state usage | Raise `mem_limit` to next row in table; raise `TM_MIN_AVAILABLE_MEM_MB` proportionally |
| HTTP 503 `host memory pressure: ...` | Host actually short on RAM (other tenants) | Find the noisy neighbor, or lower `TM_MIN_AVAILABLE_MEM_MB` carefully (close to OOM risk) |
| HTTP 503 `system load high: load_per_cpu=X > threshold 4.0` | Other tenants pegging host CPUs | Raise `cpus` (lets container compete better) OR raise `TM_MAX_LOAD_PER_CPU` (accept risk) |
| HTTP 503 `swap pressure: swap_used_pct=X% > threshold 90%` | Host swap thrash (rare; usually means RAM exhaustion) | Find the offender; do NOT just raise threshold |
| Container OOM-killed (exit 137) | `mem_limit` too low for peak ingestion (full flavor, multimodal) | Raise `mem_limit`; verify by watching `docker stats` during ingest |
| `accepting_ingest: false` permanently | One of the above thresholds is wedged | Read `system` + `thresholds` from `/health` — the warning line names the failing rule |

## Cross-checking thresholds via `/health`

The `/health` response includes the **currently-active** thresholds under
`thresholds:` (parsed from env at process start). To verify your overrides
landed:

```bash
curl -s http://localhost:8711/health | jq '{thresholds, system: .system | {cgroup_mem_limit_mb, cgroup_mem_available_mb, load_per_cpu}}'
```

Expected output after applying the 16 GB preset:

```json
{
  "thresholds": {
    "max_concurrent": 1,
    "min_available_mem_mb": 1000,
    "max_load_per_cpu": 4.0,
    "max_swap_used_pct": 90.0
  },
  "system": {
    "cgroup_mem_limit_mb": 6144,
    "cgroup_mem_available_mb": 4033,
    "load_per_cpu": 0.11
  }
}
```

If `thresholds.min_available_mem_mb` still shows `800` after you set
`TM_MIN_AVAILABLE_MEM_MB: "1000"` in compose, the env didn't propagate —
typical cause is `environment:` placed under the wrong service or compose
not picking up the override file. Run `docker compose config` to see what
compose actually resolved.

## Related

- Full env list: [`scripts/server_protection.py`](../scripts/server_protection.py)
  (`GateConfig` + `_config_from_env`)
- Compose template: [`docker-compose.override.example.yml`](../docker-compose.override.example.yml)
- 524 / Cloudflare edge timeout treatment is orthogonal — that's a network
  layer issue, not a sizing one. See operations runbook in your deployment.
