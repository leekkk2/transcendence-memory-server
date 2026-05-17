# Multi-Embedding Model Guide

> Applies to transcendence-memory-server **v0.7.0+**
> Last updated: 2026-05-16

This server is **provider-agnostic**. You can run it with:

- **One** embedding model (the v0.6.x default behavior, no YAML needed)
- **Two** embedding models routed by container name (the v0.7.0 flagship case)
- **N** embedding models with per-request override (no upper limit)
- **N** embedding models + optional reranker (Phase 2, see RERANKER_GUIDE.md)

Provider isolation is YAML-only — **no code changes** needed to add any OpenAI-`/v1/embeddings`-compatible upstream (OpenAI, Azure OpenAI, Anthropic via gateway, Voyage, Cohere via gateway, Jina, vLLM, TEI, self-hosted, custom).

---

## Quick start: single-model deployment (v0.6.x compatible)

**Zero YAML, zero new env.** Existing v0.6.x users get the legacy behavior automatically:

```bash
# .env
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_API_KEY=sk-...
```

The server synthesizes a `legacy` profile + default route. No `config/profiles.yaml` needed.

---

## Quick start: two-model deployment (most common)

### Step 1 — env

```bash
# .env
GEMINI_API_KEY=sk-newapi-...      # for upstream A
OPENAI_API_KEY=sk-...              # for upstream B
TM_PROFILES_FILE=/app/config/profiles.yaml
```

### Step 2 — config/profiles.yaml

```yaml
version: 1

embeddings:
  - name: gemini-3072
    provider: openai_compatible        # any OpenAI-/v1/embeddings-compatible upstream
    model: gemini-embedding-001
    dim: 3072
    base_url: https://newapi.example.com/v1
    api_key_env: GEMINI_API_KEY        # ← env var NAME, never the value (anti-leak)

  - name: openai-small-1024
    provider: openai_compatible
    model: text-embedding-3-small
    dim: 1024                          # OpenAI native 1536; set lower if your gateway normalizes
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

routes:
  - match: {glob: "*_openai"}          # containers named *_openai → OpenAI
    embedding: openai-small-1024

  - match: {default: true}             # everyone else → Gemini
    embedding: gemini-3072
```

### Step 3 — verify

```bash
curl -H "X-API-KEY: $RAG_API_KEY" http://localhost:8000/admin/profiles
# Expected: 2 embeddings + 1 default route + 1 glob route, api_key_configured=true

curl -X POST -H "X-API-KEY: $RAG_API_KEY" \
  "http://localhost:8000/admin/probe-embedding?profile=gemini-3072"
# Expected: {"ok":true,"latency_ms":N,"dim":3072}

curl -X POST -H "X-API-KEY: $RAG_API_KEY" \
  "http://localhost:8000/admin/probe-embedding?profile=openai-small-1024"
# Expected: {"ok":true,"latency_ms":N,"dim":1024}
```

### Step 4 — use

```bash
# This goes to gemini-3072 (default route)
curl -X POST /embed -d '{"container":"myapp","texts":["hello"]}'

# This goes to openai-small-1024 (glob *_openai)
curl -X POST /embed -d '{"container":"myapp_openai","texts":["hello"]}'

# This goes to openai-small-1024 (per-request override beats route)
curl -X POST /embed \
  -d '{"container":"myapp","texts":["hello"],"embedding_model":"openai-small-1024"}'
# ⚠ but container "myapp" was previously written with gemini-3072 (dim 3072);
#    LanceDB schema is locked, so this write WILL FAIL with PyArrow mismatch.
#    Use override only for fresh containers or matching-dim profiles.
```

---

## Profile YAML schema reference

```yaml
version: 1                              # only 1 supported

embeddings:                             # list, 1 to N
  - name: <unique-string>               # ← used in routes + per-request override
    provider: openai_compatible         # currently only this value
    model: <upstream-model-name>        # what the upstream expects in {"model": ...}
    dim: <int>                          # MUST equal what upstream actually returns
    base_url: <https://...>             # OpenAI-style /v1 base
    api_key_env: <ENV_VAR_NAME>         # indirect: env name, not value
    max_token_size: 8192                # optional, default 8192
    request_dim: null                   # optional, set to N for Matryoshka truncation
    timeout_s: 60                       # optional
    max_retries: 3                      # optional

rerankers:                              # optional list (Phase 2 feature)
  - name: <unique>
    provider: cohere_compatible         # current value; covers Cohere v2 / Jina v1 / vLLM
    model: <upstream-name>
    base_url: <https://.../v1>          # gateway must expose POST /v1/rerank
    api_key_env: <ENV_VAR_NAME>
    timeout_s: 30
    min_score: 0.0

routes:                                 # list, 1 to N + 1 default required
  - match: {exact: "container-name"}    # OR
  - match: {glob: "prefix-*"}           # OR
  - match: {regex: "^v[0-9]+_.*$"}      # OR
  - match: {default: true}              # ← exactly 1 required
    embedding: <profile-name>           # must exist in embeddings dict above
    embedding_fallbacks: []             # list of fallback profile names (Phase 3 effective; dim must match primary)
    reranker: null                      # optional, name in rerankers dict
    rerank:
      enabled: false                    # default off; per-call can override
      chunk_top_k: 30                   # first-stage retrieval pool
      top_k: 8                          # final returned count
```

---

## Provider compatibility matrix

| Provider | base_url example | provider value | Notes |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `openai_compatible` | ✓ |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deploy>` | `openai_compatible` | ✓ if Azure exposes `/embeddings` |
| Google Gemini (via OpenAI gateway like gemini-balance, newapi, oneapi) | gateway URL `/v1` | `openai_compatible` | ✓ |
| Google Gemini (native API) | `https://generativelanguage.googleapis.com/v1beta` | `openai_compatible` | ⚠ schema differs; use a gateway |
| Cohere (via gateway) | gateway `/v1` | `openai_compatible` | ✓ via newapi-style gateway |
| Voyage AI | `https://api.voyageai.com/v1` | `openai_compatible` | ✓ |
| Jina Embeddings | `https://api.jina.ai/v1` | `openai_compatible` | ✓ |
| vLLM serving embedding model | `http://your-vllm:8000/v1` | `openai_compatible` | ✓ |
| HuggingFace TEI | `http://your-tei:80` | `openai_compatible` | ⚠ check TEI exposes OpenAI route |
| Self-hosted sentence-transformers wrapper | your wrapper `/v1` | `openai_compatible` | ✓ if wrapper mimics OpenAI schema |
| Anything else | — | future `provider: native_xxx` | requires `embedding_registry._http_embed` extension (~30 LOC) |

---

## Common scenarios

### A. Add a new provider to existing deployment
1. Add env var with the API key
2. Add a `- name: ...` block in `embeddings`
3. Add a `match` block in `routes` (or use per-request override)
4. Restart server (YAML loaded at import time)
5. Probe with `/admin/probe-embedding?profile=<new-name>`

**No code changes, no recompile, no migration.**

### B. Switch all traffic to a new model
1. Backup `containers/<name>/lancedb/`
2. Create new sibling container with desired suffix matching your glob
3. Re-ingest historical data (or wait for Phase 4 migration tool)
4. Point clients at new container
5. Keep old container read-only for 30 days

### C. A/B testing two models
1. Configure both as profiles
2. Add `match: {glob: "*_v2"}` route to second profile
3. Client routes N% of traffic to `<container>_v2`
4. Compare recall@5 and latency over 3-7 days
5. Decide

### D. Emergency quota-based switch
**Trigger**: Primary upstream returning sustained 429/quota errors.
- **Quick (5s, requires restart)**: edit `profiles.yaml`, change `default` route to point to backup profile, restart container
- **Per-request (0s)**: clients add `"embedding_model": "<backup-profile>"` to each request
- ⚠ New writes use the new dim. Historical container's vec_index has old dim, so writes won't physically mix.

### E. Reduce cost (Matryoshka truncation)
For OpenAI text-embedding-3-large (native 3072), set `request_dim: 1536` or `768` to halve storage + speed up cosine. Quality drop is usually <5% for top-k retrieval. **Caveat**: existing container schema is locked at first write; truncation must be configured before first ingest.

---

## Security best practices

1. **Never inline secrets in YAML.** Always use `api_key_env: ENV_NAME` indirection.
2. **`/admin/profiles` returns `api_key_configured: bool`**, not the value. Safe to log / share.
3. **`/health` (public) contains zero profile information.** Use `/admin/system-health` for detail (requires `X-API-KEY`).
4. **`__repr__` of EmbeddingProfile / RerankerProfile redacts api_key as `'***'`**. Safe to print in debug logs.
5. **Rotate keys** by changing the env var + restart. YAML untouched.

---

## Limitations (current v0.7.0)

| Limit | Reason | Workaround |
|---|---|---|
| Same container can only hold one dim | LanceDB `fixed_size_list<float>[N]` schema lock | Use sibling containers with different names |
| Fallback profiles must match primary dim | Anti-corruption of LanceDB | Use Phase 3 fallback only for same-dim profiles |
| Only `openai_compatible` provider type | Most upstream gateways speak OpenAI schema | Extend `_http_embed` for native providers (~30 LOC) |
| YAML reload requires restart | Frozen ProfileSet design | Phase 3 may add `/admin/reload-profiles` endpoint |
| No built-in fallback runtime | Designed for v0.9.0 | Phase 3 |
| Reranker hook not yet wired | Phase 2 scope | Coming v0.8.0 |
| No automatic migration tool | Phase 4 scope | Coming v0.10.0 |

---

## Backward compatibility guarantees

| v0.6.x setup | v0.7.0 behavior |
|---|---|
| Only EMBEDDING_* env, no YAML | ✓ Synthesizes `legacy` profile, default route — identical behavior |
| Old LanceDB rows without `embedding_model` metadata | ✓ Read with `None`, behavior unchanged |
| Client code without `embedding_model` field | ✓ Falls through to route resolution |
| Existing container vec_index | ✓ Preserved; new metadata only on new rows |

---

## How the reranker actually fires (and when it does not)

Reranker is **silent by default**. Two independent preconditions must be true:

| Layer | Required state | Verification |
|---|---|---|
| **Config** | Route resolved for the request has `reranker: <name>` set AND `rerank.enabled: true`, OR the per-call request body sets `"rerank": true` | `curl $SRV/admin/profiles` shows the route's `reranker` and `rerank.enabled` |
| **Query path** | Client called `POST /query` (not `POST /search`) | server access log shows `/query`, not `/search` |

If **either** is missing, reranker silently does nothing:

- `POST /search` is a direct LanceDB cosine + topk path. It never invokes a reranker, regardless of config.
- A route with `rerank.enabled: false` (the default) only fires the reranker when the request body includes `"rerank": true`.

### Data path is not a precondition

Reranker operates on whatever chunks LightRAG returns from retrieval, regardless of source. `/query` works on both:

- **Knowledge-graph content** ingested via `POST /documents/text` or `POST /documents/upload` (LightRAG native path)
- **LanceDB-only content** ingested via `POST /ingest-memory/objects` or `/tm remember` — LightRAG hybrid mode falls back to LanceDB vector retrieval when the knowledge graph is empty

Either way, the retrieved chunks are then handed to the reranker before LLM synthesis. Verify by checking server logs for `Successfully reranked: N chunks from M original chunks` after any `/query` call.

### Per-request field name (Pydantic strict)

The request body field is `rerank: bool` (not `enable_rerank`). Sending an unknown key like `enable_rerank: true` is silently ignored by Pydantic — the reranker stays off, no warning emitted.

```bash
# Correct — overrides route default
curl -X POST $SRV/query -H 'Content-Type: application/json' \
  -d '{"container":"my-container","query":"...","rerank":true}'

# Wrong — silently ignored
curl -X POST $SRV/query -H 'Content-Type: application/json' \
  -d '{"container":"my-container","query":"...","enable_rerank":true}'
```

### Three ways to turn reranker on

| Mode | Change | When to use |
|---|---|---|
| Per-request | Add `"rerank": true` to `/query` body | You already use `/query` and want it for one call |
| Default-on for a route | Set `rerank.enabled: true` in the route's `rerank:` block in `profiles.yaml` | All `/query` traffic on that route should rerank (note: adds ~200-500 ms per call) |
| Single-container test | Add a glob route like `{glob: "*_rerank"}` with `rerank.enabled: true`; ingest a test container ending in `_rerank` and compare | A/B testing reranker impact |

### Optional: dual-write for richer `/query` answers

`/query` works on LanceDB-only containers (via hybrid-mode fallback retrieval), but answer quality is highest when content also lives in the knowledge graph. For containers that will be queried via `/query` (not just `/search`), consider also writing through `POST /documents/text` so LightRAG can use entity / relation extraction in addition to vector retrieval.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `/admin/profiles` 500 on startup | YAML parse error or dim mismatch in fallback | check `/var/log/...` for `_validate` error |
| `/admin/probe-embedding?profile=X` returns 503 | Upstream gateway error | curl the upstream directly to isolate |
| `/admin/probe-embedding?profile=X` returns 401 | Wrong api_key or env not set | check `api_key_configured: true` in `/admin/profiles` |
| Worker subprocess fails with `embedding profile 'X' not found` | YAML doesn't define profile X | check `/admin/profiles` for actual names |
| Sudden dim mismatch on write | Per-request override targeted wrong-dim profile | only override matching-dim profiles for existing containers |
| Backwards-incompat after upgrade | Edge case in legacy synthesis | report with `EMBEDDING_*` env dump (redact key) |
| `/query` returns `(no answer generated)` | Either container is empty, or retrieval matched 0 chunks | check `curl /admin/system-health` for container size; widen query |
| Reranker configured but never fires | Traffic is on `/search` (LanceDB only) | switch to `/query` for the requests you want reranked |
| Reranker upstream returns 400 on `/v1/rerank` | Wrong model name in `rerankers[].model` — some gateways (e.g. newapi) accept only specific names for the rerank endpoint, even if the channel advertises multiple model names | curl `POST /v1/rerank` directly with both candidate names to find the one the gateway accepts |
| `enable_rerank` field has no effect | Wrong field name; Pydantic silently drops unknown keys | use `rerank: true` (not `enable_rerank: true`) |

---

## See also

- [`config/profiles.yaml.example`](../config/profiles.yaml.example) — full reference
- [`MIGRATION_EMBEDDINGS.md`](MIGRATION_EMBEDDINGS.md) — switch a container's embedding profile in-place
- [`operations/troubleshooting.md`](operations/troubleshooting.md) — runtime symptom catalog
