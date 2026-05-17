# Known Issue: LightRAG NanoVectorDB Files Are Not Container-Isolated

**Status:** open · **Severity:** high (cross-container correctness) · **First documented:** 2026-05

## Symptom

A `POST /query` call against container `A` (using an embedding profile with
dim `X`) returns HTTP 500 with the following stack trace in the server log:

```
File ".../nano_vectordb/dbs.py", line 73, in __post_init__
    storage["embedding_dim"] == self.embedding_dim
AssertionError: Embedding dim mismatch, expected: X, but loaded: Y
```

The crash is deterministic and happens at LightRAG initialization, before
retrieval / reranker / LLM steps. Other containers using embedding profile
with dim `Y` continue to work; the crash is one-directional.

## Root cause

LightRAG's `NanoVectorDB` backend persists its on-disk state to:

- `<lightrag-working-dir>/vdb_chunks.json`
- `<lightrag-working-dir>/vdb_entities.json`
- `<lightrag-working-dir>/vdb_relationships.json`
- `<lightrag-working-dir>/graph_chunk_entity_relation.graphml`
- `<lightrag-working-dir>/kv_store_*.json`

The server code in `scripts/rag_engine.py` correctly computes a
per-container path with `_container_working_dir(container)` and passes it
as `working_dir=` to `LightRAG(...)`. However, in the current LightRAG +
NanoVectorDB combination shipped with the image, those `vdb_*.json` /
`graph_*.graphml` / `kv_store_*.json` files are created at the **top level
of `/data/`** rather than under the per-container subdirectory. The first
container to issue `/query` populates those files with its own
`embedding_dim`, and every subsequent container with a different dim
then crashes the dim-mismatch assertion above.

This is a vector-store *isolation* bug, not a config bug — there is no
per-container or per-route knob in the current build that fixes it.

## Impact

- Any deployment that runs multiple containers with **different embedding
  dims** through `/query` will hit this. Single-dim deployments are safe.
- `POST /search` is unaffected — it reads LanceDB chunks directly and does
  not load NanoVectorDB.
- The first dim to land "wins" the global files. Containers matching the
  losing dim are stuck on `/query` until the files are wiped.

## Workaround (temporary)

Pick the dim you want to keep, back up the others, and let LightRAG
re-materialize the files on the next `/query`:

```bash
# Inside the rag-server container:
docker exec -it <rag-server> bash -c '
  cd /data
  TS=$(date +%Y%m%d-%H%M%S)
  mkdir -p _vdb-backup/$TS
  mv vdb_*.json _vdb-backup/$TS/        2>/dev/null
  mv graph_*.graphml _vdb-backup/$TS/   2>/dev/null
  mv kv_store_*.json _vdb-backup/$TS/   2>/dev/null
'
```

⚠ **This wipes the global LightRAG knowledge graph for all containers**,
not just the offending one. Any entity / relation extraction prior to this
point will be re-derived from scratch on the next ingest.

## Permanent fix (planned)

The clean fix is to make NanoVectorDB respect LightRAG's `working_dir`
when computing `storage_file`. Two paths:

1. Upstream patch to `nano_vectordb` / `lightrag.kg.nano_vector_db_impl`
   ensuring `os.path.join(self.global_config["working_dir"], "vdb_*.json")`
   is used uniformly, then bump the pinned LightRAG version.
2. Replace NanoVectorDB with a backend that has stronger isolation (Milvus,
   Qdrant, or LanceDB) per container, configured in
   `lightrag_kwargs["vector_storage"]`.

Until either lands, the operational rule is: **do not enable `/query` for
containers whose embedding dim differs from the dim already "owned" by
`/data/vdb_*.json`**. Inspect ownership with:

```bash
docker exec -it <rag-server> python3 -c "
import json, sys
for p in ['/data/vdb_chunks.json', '/data/vdb_entities.json', '/data/vdb_relationships.json']:
    try:
        d = json.load(open(p))
        print(p, 'dim=', d.get('embedding_dim'), 'rows=', len(d.get('data', [])))
    except FileNotFoundError:
        print(p, 'missing')
"
```

## Related

- `docs/MULTI_MODEL_GUIDE.md` — multi-embedding-profile configuration.
- `docs/operations/rerank-perf-baseline.md` — the test that uncovered this
  bug (a 1024-dim container retried `/query` after a 3072-dim container
  had populated the globals).
