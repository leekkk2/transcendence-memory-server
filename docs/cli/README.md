# `tm` CLI — User Guide

`transcendence-memory-cli` (entry point `tm`) is a shell-friendly client for the
self-hosted transcendence-memory RAG memory service. It is a sibling to the
Claude Code `transcendence-memory` skill and shares the same configuration file
(`~/.transcendence-memory/config.toml`).

## Install

```bash
pipx install transcendence-memory-cli
# or for editable / from-source work:
cd cli-package && pip install -e .
```

After install, verify:

```bash
tm --help
tm version
```

## Pair with a server

You need a base64 *connection token* emitted by the server's
`/export-connection-token` endpoint. Ask the server operator, or — if you
already have an API key — generate one yourself:

```bash
curl -H "X-API-KEY: ***" \
     -H "User-Agent: transcendence-memory-cli/0.1.0" \
     https://your-rag.example.com/export-connection-token?container=your-project \
     | jq -r .token
```

Then on the consuming machine:

```bash
tm connect <PASTE-TOKEN-HERE>
tm status
```

`tm connect` writes a 0600-permission `config.toml`:

```toml
[connection]
endpoint  = "https://your-rag.example.com"
container = "your-project"

[auth]
mode    = "api_key"
api_key = "***"
```

## Command matrix

| Command | What it does |
|---|---|
| `tm connect <token>` / `--manual` | Save endpoint + container + API key to `config.toml` |
| `tm status` | `GET /health` summary |
| `tm search <q> [--topk N]` | Search the configured container |
| `tm search --all <q>` | Search every container (glob `*`) |
| `tm search --match <pattern> <q>` | Search a name pattern |
| `tm remember <text> [--tags a,b]` | Store one memory in the configured container |
| `tm update <id> [text] [--tags ...]` | Patch an existing memory object |
| `tm delete <id> [--yes]` | Delete a memory object |
| `tm embed [--container-name name] [--async]` | Trigger a re-embed |
| `tm query <question> [--mode hybrid]` | RAG-Anything answer |
| `tm upload <file> [--parse-method ...]` | Multimodal document ingest |
| `tm containers [pattern]` | List containers + index state |
| `tm batch <file.jsonl> [--redact --resume]` | Bulk JSONL ingest |
| `tm export-token` | Print a base64 token built from the local config |
| `tm config show` / `tm config set <k> <v>` | Inspect / mutate `config.toml` |
| `tm version` | CLI (and reachable server) version |
| `tm --install-completion bash/zsh/fish` | Install shell completion (typer built-in) |

## Global options

| Option | Default | Notes |
|---|---|---|
| `--endpoint` | from config | One-off override |
| `--container` / `-c` | from config | One-off override |
| `--api-key` | from config | Scripts only — never commit |
| `--json` | off | Print raw JSON for `jq` pipelines |
| `--quiet` / `-q` | off | Suppress stdout, rely on exit code |
| `--verbose` / `-v` | off | Print HTTP request/response traces |
| `--no-color` | off | Disable rich color (CI-friendly) |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Argument / usage error |
| `2` | Authentication failed (401 / 403) |
| `3` | Connection / network failure |
| `4` | Server error (>= 500 or unexpected 4xx) |
| `5` | Required config missing — run `tm connect` first |

## Configuration

Resolution priority (highest → lowest):

1. CLI flags (`--endpoint`, `--container`, `--api-key`)
2. Env vars `TM_ENDPOINT`, `TM_CONTAINER`, `TM_API_KEY`
3. `~/.transcendence-memory/config.toml`

`tm config show` masks the API key. `tm config set api_key <value>` rewrites
the file in place (atomic replace + `chmod 0600`).

## Pipelines

```bash
# Top-3 search hits as plain JSON
tm search "docker port conflict" --topk 3 --json | jq '.results[] | .text'

# Pipe a JSONL ingest into the default container with redaction
tm batch ./notes.jsonl --redact --resume

# Quick "is the server reachable?" in CI
tm --quiet status && echo "OK"
```

## Coexisting with the Claude Code skill

The skill and the CLI share `config.toml`. Pair once via either; the other side
picks up the same credentials automatically. The skill provides slash-command
ergonomics inside Claude Code; the CLI provides shell + script ergonomics.

## Cloudflare WAF / User-Agent

`tm` always sends `User-Agent: transcendence-memory-cli/<version>` so endpoints
fronted by Cloudflare with the default "block unknown Python UA" rule (1010)
accept the requests. No further configuration needed.

## Reporting issues

File issues at
<https://github.com/leekkk2/transcendence-memory-server/issues> — include the
output of `tm --verbose status` (redact your API key first).
