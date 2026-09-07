# Container reconciliation and embedding integrity

Back up the stopped service's complete data volume and deployment configuration.
Verify the archive hash and retain an independent copy before changing data.
Reconciliation writes only to a separate workspace; it never deletes source data.

```sh
python scripts/reconcile_containers.py \
  --source-workspace /path/to/snapshot \
  --output-workspace /path/to/candidate \
  --sources old-container current-container --target current-container \
  --profile approved-profile
# Review the plan, then add --commit. Add --resume after an interrupted build.
```

The script preserves every JSONL row, including historical repeated IDs. It
checks source line IDs against indexed chunks, adjusts appended chunk line
numbers, and regenerates every vector using one explicit profile without fallback.
It retains the first source's graph and governance artifacts. A second source
containing a graph requires separate graph reconciliation and is rejected.

During cutover, stop all writers, verify source JSONL hashes still match the
plan, and retain the old container directories outside the active `containers/`
directory. Install the candidate, update metadata/index state and aliases, then
restart. Keep deprecated old names pointing directly at the canonical container.
If data changed while building, stop the cutover and reconcile the delta first.
Never restore a stale whole database over new unrelated writes.

Validate source rows as a multiset, final vector count/dimension/model/profile,
finite vector values, exact-document and representative semantic queries, old
alias reads, and actual new writes. Rollback swaps back retained directories and
restores only the changed configuration/metadata; preserve any post-cutover writes.

## Vector-space contract

A model alias can refer to a self-hosted model. Record the approved upstream
channel, underlying model, output dimensions and input-processing strategy.
Equal dimensions are insufficient for compatibility. The loader rejects
cross-model/provider embedding fallback. Same-model replicas still require
operator verification that their model revision and preprocessing match.
Historical model labels may be inaccurate if an earlier fallback generated them;
compare real vectors or regenerate them through the approved channel.

Optional gateway limits default to zero (disabled):

- `TM_EMBEDDING_MAX_INPUT_CHARS`: split overlong inputs by Unicode characters.
- `TM_EMBEDDING_MAX_BATCH_SIZE`: bound the number of inputs per request.

All characters are retained. Multi-part vectors use a length-weighted mean,
then unit normalization; a single part is unchanged. The worker and async
retrieval share this transform. Changing its settings requires rebuilding the
index. This follows the chunk-and-average pattern in the
[OpenAI long-input embedding cookbook](https://developers.openai.com/cookbook/examples/embedding_long_inputs),
using character bounds to match a gateway's character limit.

## Errors and governance

`GET /admin/usage/errors?window=24h&category=other` excludes unauthenticated
404s. Other categories: `all`, `authenticated`, `unauthenticated_404`.
Use `limit` (1–200), `offset`, `path` and `container` for drill-down. Authentication
is required. Entries include HTTP status, sanitized diagnostic fields, timestamp,
container, latency and a generated request ID. Validation inputs and credentials
are excluded. Historical entries without bodies stay explicitly unavailable.
Tool failures returned with HTTP 200 are logged with `error_type=tool`.
Raw historical counts are retained; summary adds unauthenticated-404 and
API-credential error counts. Credential presence does not prove authentication.

The dashboard selects one target container for all tools. Parameters are JSON;
routing requires `{"rules": {...}}`. Routing updates preserve existing keys both
across containers and inside the selected container's rules. This governance
blob does not replace `profiles.yaml` model routing or the container-alias API.

Dry-run all five tools before applying any. Compression uses the configured
LLM gateway and appends index cards; source memories remain. Model tuning has
an allow-list and affects global retrieval defaults; inspect its preview first.
Quarantine execution requires reviewing the plan and explicit confirmation.
Configuring/enabling this tool does not authorize quarantining actual memories.

## Publishing unchanged dependencies efficiently

When only application code changes, `scripts/build-runtime-layer.sh` uses
Google's `go-containerregistry` **crane append** to create a deterministic
application layer locally and reuse the existing pinned runtime layers in the
same registry. It does not rebuild or download the large dependency stack.
Build the dashboard first and require a clean committed tree. Supply
`TM_BASE_IMAGE` (digest), `TM_TARGET_IMAGE` (unique tag), and `CRANE` (binary).
Registry credentials belong in a restricted temporary `DOCKER_CONFIG` directory,
never arguments or tracked files. Validate the resulting full runtime in an
isolated candidate container before changing production, then pull by digest.
Dependency changes require the regular Dockerfile build instead of this path.
