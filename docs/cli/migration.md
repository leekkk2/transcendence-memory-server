# Migrating from raw `curl` to `tm`

If you previously hand-rolled `curl` calls against the transcendence-memory
server, the `tm` CLI is a drop-in replacement for the most common workflows.
This page maps the old commands to the new ones.

## Setup

Before:

```bash
export RAG_ENDPOINT=https://your-rag.example.com
export RAG_API_KEY=***
export RAG_CONTAINER=your-project
```

After:

```bash
tm connect <BASE64-TOKEN>
# or, manual:
tm connect --manual
```

The CLI persists settings to `~/.transcendence-memory/config.toml` so you
never need to re-export them in a new shell.

## Status / health

| Before | After |
|---|---|
| `curl -H "X-API-KEY: $RAG_API_KEY" -H "User-Agent: transcendence-memory-cli/0.1.0" "$RAG_ENDPOINT/health"` | `tm status` |

## Search

```bash
# Before
curl -sS -H "X-API-KEY: $RAG_API_KEY" \
     -H "User-Agent: transcendence-memory-cli/0.1.0" \
     -H "Content-Type: application/json" \
     -d '{"query":"docker","topk":5,"container":"your-project"}' \
     "$RAG_ENDPOINT/search" | jq

# After
tm search "docker" --topk 5 --json | jq
```

## Remember a single memory

```bash
# Before — must hand-build the JSON envelope + supply a unique id.
curl -sS -H "X-API-KEY: $RAG_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"container":"your-project","objects":[{"id":"mem-001","text":"hello"}],"auto_embed":true}' \
     "$RAG_ENDPOINT/ingest-memory/objects"

# After
tm remember "hello"
```

## Bulk ingest

The CLI ships a `batch` command equivalent to the skill's
`scripts/batch-ingest.py`:

```bash
# Before
python3 batch-ingest.py "$RAG_ENDPOINT" "$RAG_API_KEY" "$RAG_CONTAINER" notes.jsonl --redact --resume

# After
tm batch notes.jsonl --redact --resume
```

## Document upload

```bash
# Before
curl -H "X-API-KEY: $RAG_API_KEY" \
     -H "User-Agent: transcendence-memory-cli/0.1.0" \
     -F "container=your-project" \
     -F "file=@./report.pdf" \
     "$RAG_ENDPOINT/documents/upload"

# After
tm upload ./report.pdf
```

## RAG-Anything query

```bash
# Before
curl -sS -H "X-API-KEY: $RAG_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"query":"summarize Q3 incident postmortems","container":"your-project","mode":"hybrid"}' \
     "$RAG_ENDPOINT/query" | jq .answer

# After
tm query "summarize Q3 incident postmortems" --json | jq .answer
```

## Moving to a new device

```bash
# Old laptop
tm export-token > token.b64

# New laptop (after `pipx install transcendence-memory-cli`)
tm connect "$(cat token.b64)"
tm status
```

That copies endpoint, container, and the API key in a single base64 blob — same
format the server's `/export-connection-token` endpoint produces.

## Common pitfalls

* **Cloudflare WAF 1010** — the CLI sets `User-Agent` by default; raw `curl`
  must do the same.
* **Stale results** — `tm remember` triggers a background embed by default but
  results are not searchable until the embed converges. Use `tm embed` to
  force a full rebuild after a bulk import.
* **Config drift** — if `~/.transcendence-memory/config.toml` exists on a host
  but you want a one-off override, prefer `--endpoint` / `--container` /
  `--api-key` flags over rewriting the file.
