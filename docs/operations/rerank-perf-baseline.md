# Reranker Performance Baseline

End-to-end perf characteristics of reranker integration when running behind
an OpenAI-compatible aggregator gateway. Numbers below are intended as
ballpark figures to set expectations; absolute latency depends on the
specific upstream provider, network RTT, and concurrent load.

## Test setup

- Reranker provider: `text-embedding-3-small` model routed through `/v1/rerank`
  on an OpenAI-compatible aggregator (cosine-similarity-based pseudo-rerank).
- LightRAG `hybrid` mode with default `chunk_top_k=30`, `top_k=8`.
- LLM: `gpt-5.4-mini` for entity/relation extraction + answer composition.

## 1. Direct `/v1/rerank` latency (sequential)

| documents | top_n | trial 1 (cold) | trials 2-5 (warm) |
|---|---|---|---|
| 10  | 5  | ~1700 ms | ~290 ms |
| 30  | 8  | ~300 ms  | ~295 ms |
| 100 | 10 | ~380 ms  | n/a |

The latency is largely insensitive to document count — going from 10 to 100
documents adds only ~30%. Cold start (~1.5-1.7 s) is dominated by DNS + TLS +
HTTP connection pool init. Subsequent warm calls stabilize below 400 ms.

## 2. Concurrency (10 in-flight requests)

| metric | value |
|---|---|
| concurrency | 10 |
| total calls | 20 |
| wall clock | ~1.6 s |
| ok / err | 20 / 0 |
| latency min / P50 / P95 / max | 308 / 805 / 1105 / 1105 ms |
| throughput | ~12-13 req/s |

The first batch (10 cold parallel calls) lands at 800-1100 ms; subsequent
batches reuse warm connections and drop to ~310 ms each. No 429 / 5xx
errors observed at this level.

## 3. End-to-end `/query` overhead (rerank on vs off)

5-query A/B on a small KG-backed container (`top_k=3`, `hybrid` mode):

| mode | avg latency | min / max | answer length |
|---|---|---|---|
| `rerank: true`  | ~1360 ms | 1210 / 1710 | ~420 chars |
| `rerank: false` | ~800 ms  | 760 / 850   | ~400 chars |
| **delta** | **+560 ms / call** | — | +16 chars / call |

All 5 reranked calls triggered `INFO: Successfully reranked: N chunks from
M original chunks` in server logs. Zero rerank errors.

> Caveat: on tiny corpora (≤ 10 chunks total) the retrieval step rarely
> reaches `chunk_top_k=30` candidates, so reranking degenerates to a
> near-no-op ("1 chunk in, 1 chunk out"). Re-run this benchmark on a corpus
> with ≥ 50 chunks to see the rerank step do meaningful re-ordering work.

## 4. Pseudo-rerank precision (direct `/v1/rerank` probe)

Multiple query types against a fixed 5-document set (1 target + 4 noise):

| query type | top-1 hit | top-1 score | top-2..5 score | separation |
|---|---|---|---|---|
| Exact keyword                | correct | 0.984 | ~1.6e-5 | ~61 000× |
| Semantic paraphrase          | correct | 0.865 | ~1.6e-5 | ~54 000× |
| Topic switch                 | correct | 0.914 | ~1.6e-5 | ~57 000× |
| 100-document realistic query | correct | 0.940 | 0.17 / 0.11 | continuous distribution above noise floor |

Pseudo-rerank is excellent at separating "the most relevant document" from
noise (massive score gap), but on small candidate sets it behaves more like
a binary classifier than a continuous ranker — tied near-zero scores for
ranks 2..N. On larger candidate sets (≥ 30 docs) a meaningful score
distribution emerges, but it still lags a true cross-encoder reranker
(Cohere / BGE / Jina) in fine-grained ranking quality.

## 5. Production recommendations

- **Default routes**: keep `rerank.enabled: false` and let callers opt in with
  per-request `"rerank": true`. This avoids the +500 ms tax on traffic that
  doesn't need it.
- **A/B testing**: add a `{glob: "*_rerank"}` route with `rerank.enabled: true`
  and dual-ingest the same content to compare answer quality side by side.
- **Latency budget**: plan ~+300-600 ms per `/query` call when reranker is
  on (warm path). Cold start adds another ~1-1.5 s for the first call after
  idle.
- **Throughput ceiling**: ~12-13 req/s for a single backend process behind
  one gateway channel. For higher throughput, scale the gateway channel
  weight or add a second channel.
- **Embedding-only path bypass**: `POST /search` is a direct LanceDB cosine
  query — it never goes through LightRAG / reranker. Route requests that
  should be reranked to `POST /query`.

## See also

- `docs/MULTI_MODEL_GUIDE.md` — reranker configuration and trouble table.
- `docs/operations/known-issue-global-vdb-isolation.md` — current LightRAG
  vector-store isolation limitation that affects multi-dim containers.
- `config/profiles.yaml.example` — full reference configuration.
