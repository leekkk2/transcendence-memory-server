# Embedding migration guide

This guide covers `scripts/migrate_embeddings.py` — the tool for re-embedding a
container's LanceDB `chunks` table from one embedding profile (model + dim) to
another. Use it whenever you need to change the embedding model for a container
in a way that breaks the existing vector dimension (e.g. Gemini 3072-dim →
OpenAI 1024-dim) or when you simply want to rebuild all vectors with a
different provider.

---

## When do you need migration?

| Scenario | Migration required? | Why |
| --- | --- | --- |
| Switching dim (3072 → 1024) | **Yes** | LanceDB stores vectors as `fixed_size_list<float>[N]`; mixed dims are impossible inside one table. |
| Switching provider, same dim | Optional | Search still works (numerically), but mixed embedding spaces hurt recall. Recommended. |
| Adding a fallback at the same dim | No | Use `embedding_fallbacks` in `profiles.yaml`. Both primary and fallback share the table. |
| Upgrading model within the same family (e.g. `text-embedding-3-small` → `-3-large`) | **Yes** | Different model = different vector space, even if dim happens to match. |
| Routing a brand-new container to a different profile | No | New containers create a fresh table at the profile's dim. |

---

## CLI

```
python scripts/migrate_embeddings.py \
    --container <name> \
    --from <profile-name> \
    --to   <profile-name> \
    [--batch-size 100] \
    [--dry-run | --commit] \
    [--workspace /path/to/workspace] \
    [--json]
```

- `--container` — container name (matches the folder `tasks/rag/containers/<name>/`).
- `--from` / `--to` — profile names that exist in your `config/profiles.yaml`
  (resolved by `EmbeddingRegistry.get_profile`).
- `--batch-size` — rows per embed call (default 100). Larger = faster but more
  RAM and a longer single failure window.
- `--dry-run` — **default behaviour**. Estimates work without writing.
- `--commit` — actually performs the migration.
- `--workspace` — defaults to `$WORKSPACE` env, else repo root.
- `--json` — print a machine-readable JSON summary in addition to the markdown
  table.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (dry-run or commit) |
| 1 | Configuration error (profile missing, container not found) |
| 2 | Runtime error during embedding / writing |
| 3 | State conflict (`chunks_v2` already exists — see "Recovery") |

---

## Workflow

### Step 0 — Back up the container directory

`migrate_embeddings.py` preserves the old table as `chunks_old_<timestamp>`,
but a full tarball is cheap insurance against orchestration mistakes:

```bash
tar czf ~/backup/<container>-$(date +%Y%m%d).tgz \
    tasks/rag/containers/<container>
```

**Do not skip this step.** Once you `--commit`, the old vectors only exist in
the renamed `chunks_old_*` directory inside the same LanceDB folder — a
`rm -rf` mistake there is unrecoverable without a backup.

### Step 1 — Dry run

```bash
python scripts/migrate_embeddings.py \
    --container imac \
    --from gemini-3072 \
    --to   openai-small-1024
```

You'll see something like:

```
# Migration dry-run summary

- **from profile**: `gemini-3072`
- **to profile**: `openai-small-1024`

| Metric | Value |
| --- | --- |
| total rows | 5751 |
| batch size | 100 |
| batch count | 58 |
| sample size | 5 |
| sample latency (ms) | 312.4 |
| per-text latency (ms) | 62.5 |
| estimated duration | 6.0m |
| estimated tokens | 1,420,500 |

Re-run with `--commit` to perform the actual migration.
```

The sample latency is measured by actually calling the `--to` profile once
with up to 5 real rows from the table, so the duration estimate reflects your
current network / upstream conditions.

### Step 2 — Commit

```bash
python scripts/migrate_embeddings.py \
    --container imac \
    --from gemini-3072 \
    --to   openai-small-1024 \
    --commit
```

Progress is printed to **stderr**, the final summary to **stdout**:

```
batch 1/58 embedded 100 rows in 6217 ms (cumulative 100/5751)
batch 2/58 embedded 100 rows in 6042 ms (cumulative 200/5751)
...
batch 58/58 embedded 51 rows in 3120 ms (cumulative 5751/5751)

# Migration commit summary

- **container**: `imac`
- **from profile**: `gemini-3072`
- **to profile**: `openai-small-1024`

| Metric | Value |
| --- | --- |
| migrated rows | 5751 |
| total rows (src) | 5751 |
| new vector dim | 1024 |
| new model | `text-embedding-3-small` |
| elapsed | 6.2m |
| batches | 58 (size 100) |
| old table backup | `tasks/rag/containers/imac/lancedb/chunks_old_20260516_153012.lance` |
| new active table | `tasks/rag/containers/imac/lancedb/chunks.lance` |

Old table preserved — verify search results, then remove manually:
`rm -rf tasks/rag/containers/imac/lancedb/chunks_old_20260516_153012.lance`
```

### Step 3 — Update routing

Edit `config/profiles.yaml` so the container routes to the new profile:

```yaml
routes:
  - match: {exact: imac}
    embedding: openai-small-1024     # was gemini-3072
```

Restart the server so `EmbeddingRegistry` reloads.

### Step 4 — Smoke test

Issue a few search queries (`POST /tasks/rag/search` or your usual smoke
script). Confirm:

1. Results come back without 4xx/5xx.
2. Recall is sane (compare against a memorized query).
3. Vector dim in any debug endpoint matches the `--to` profile.

### Step 5 — Remove old backup

Only after confirming results:

```bash
rm -rf tasks/rag/containers/imac/lancedb/chunks_old_20260516_153012.lance
```

There is no `--cleanup` flag on the tool, deliberately — manual deletion
keeps you from accidentally losing the rollback option.

---

## Rollback

If smoke tests look wrong:

```bash
cd tasks/rag/containers/imac/lancedb
mv chunks.lance chunks_failed_$(date +%Y%m%d_%H%M%S).lance
mv chunks_old_20260516_153012.lance chunks.lance
```

Then revert `profiles.yaml` and restart. The old vectors are untouched and
fully usable.

---

## Multi-container migrations

The tool intentionally accepts one container at a time. For batches:

```bash
for c in imac yzjx test-alpha; do
    python scripts/migrate_embeddings.py \
        --container "$c" \
        --from gemini-3072 \
        --to   openai-small-1024 \
        --commit
done
```

Loop sequentially (one container at a time) so a single upstream quota error
doesn't abort multiple migrations in parallel. Each container's old backup is
kept until you delete it.

---

## Difference vs `embedding_fallbacks`

| Aspect | `embedding_fallbacks` (in `profiles.yaml`) | `migrate_embeddings.py` |
| --- | --- | --- |
| When it runs | Per-request, at search/ingest time | One-off, batch operation |
| dim change | **Not allowed** — loader enforces same dim across primary + fallbacks | **Required reason** to run — re-embeds the table at the new dim |
| Data rewrite | None — same table | All vectors re-computed and rewritten |
| Use case | Provider redundancy (Gemini 429 → OpenAI takeover) | Permanent provider/model switch |

`embedding_fallbacks` is for hot-path resilience; `migrate_embeddings.py` is
for permanent schema change. They are complementary, not alternatives.

---

## Troubleshooting

### `chunks_v2 already exists`

A previous `--commit` was interrupted. Inspect the staging table — it may
contain partial data:

```bash
ls tasks/rag/containers/<name>/lancedb/
# Look for chunks_v2.lance — that's the half-finished migration.
```

Decide:

- **Throw it away and retry**:
  `rm -rf tasks/rag/containers/<name>/lancedb/chunks_v2.lance` then rerun
  `--commit`.
- **Keep it for forensics**: rename it (e.g. to `chunks_v2_failed_<ts>.lance`)
  before retrying.

### Out of memory

Lower `--batch-size`. Each batch holds the full text + embedded vectors in
memory until the LanceDB write completes. For 3072-dim float32 vectors,
batch=100 ≈ 1.2 MiB of vector data per batch; the dominant cost is the row
metadata and text payloads.

### Upstream quota / 429 / 5xx

The tool delegates retry to `embedding_registry._http_embed` (3 retries with
exponential backoff + Retry-After). A *sustained* outage will eventually raise
and abort with exit code 2. The staging table is left in place so you can
inspect how far you got:

```bash
python -c "
import lancedb
t = lancedb.connect('tasks/rag/containers/<name>/lancedb').open_table('chunks_v2')
print('staged rows:', t.count_rows())
"
```

Then delete it and retry from scratch (the tool is not resumable mid-batch).

### Network blackhole / SSL errors

Check `--to` profile's `base_url` and `api_key_env` value. The dry-run path
calls the actual upstream once, so a misconfigured profile shows up before
you `--commit`.

### Dim mismatch error: "table vector schema is dim=X but --from has dim=Y"

You passed the wrong `--from` profile, or the table predates v0.7.0 (no
`embedding_*` metadata fields). Confirm with:

```bash
python -c "
import lancedb
t = lancedb.connect('tasks/rag/containers/<name>/lancedb').open_table('chunks')
print('schema dim:', t.schema.field('vector').type.list_size)
print('first row meta:', {k: t.to_arrow().slice(0,1).to_pylist()[0].get(k) for k in ('embedding_model','embedding_dim','embedding_profile')})
"
```

---

## Important warnings

1. **Always run `--dry-run` first** — it surfaces auth errors, model name
   typos, and quota issues before you start writing.
2. **Always back up the container directory before `--commit`** — `tar czf`
   is cheap, restoring from nothing is impossible.
3. **The vector index (IVF/HNSW) is not migrated.** Once the new table is in
   place, the next ingest or a manual `optimize()` rebuilds the index at the
   new dim. Search latency will be slightly higher until then.
4. **Do not run two `--commit` migrations on the same container in parallel.**
   The tool refuses if `chunks_v2` exists, but file-system race conditions can
   still corrupt the staging table. Sequence them.
5. **Update `profiles.yaml` routing after migration** — otherwise the next
   ingest writes to the new table with the *old* profile's embedding values
   (because container routing still points to the old profile), creating a
   mixed-vector table.
