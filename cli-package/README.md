# transcendence-memory-cli

Shell CLI for the transcendence-memory self-hosted RAG memory service.

```bash
pipx install transcendence-memory-cli
tm connect <token>
tm status
tm search "docker port conflict"
tm remember "Daily standup at 09:30 UTC"
```

Same `~/.transcendence-memory/config.toml` as the Claude Code skill — pair once,
use everywhere.

See `docs/cli/README.md` in the upstream server repository for the full reference.

## Entry point

After install:

```bash
tm --help
```

## License

MIT.

