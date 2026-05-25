# Contributing

Thanks for your interest in contributing to transcendence-memory-server!

## Development Setup

```bash
git clone https://github.com/leekkk2/transcendence-memory-server.git
cd transcendence-memory-server
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest tests/ -v
```

## Code Style

- Python 3.11+, type hints encouraged
- Keep functions under 50 lines
- Use meaningful names, avoid magic numbers

## Naming Conventions (open-source hygiene)

This is a public open-source repository. **Do not commit vendor-specific business identifiers** — internal scope codes, internal product names, deploy hostnames, device names, sprint codes, etc. Use generic placeholders in code, tests, comments, and commit messages:

| Don't write | Use instead |
|---|---|
| Internal scope codes (e.g. private team / personal credential labels) | `personal` / `team` / `shared` |
| Internal product / app names | `your-app` / `example-app` |
| Specific deploy hostnames | `example-host` / `memory.example.com` |
| Specific device names | `device-x` / `host-y` |
| Internal sprint codes (e.g. `XX-NNN`) | drop them or use generic `cleanup-YYYY-MM` |

### Pre-commit guard

A one-shot guard checks for the most common leakable patterns. Wire it up locally:

```bash
git config core.hooksPath .githooks
# Now every git commit runs scripts/check-no-private-identifiers.sh on staged files
```

You can also run it ad-hoc against the full tree:

```bash
bash scripts/check-no-private-identifiers.sh
```

If you intentionally need one of the flagged words (e.g. a legitimate code example), add a tightly-scoped allow comment and propose the rule update in the PR.

## Pull Requests

1. Fork the repo and create a feature branch
2. Write tests for new functionality
3. Ensure all tests pass
4. Submit a PR with a clear description

## Reporting Issues

Use [GitHub Issues](https://github.com/leekkk2/transcendence-memory-server/issues) with:
- Steps to reproduce
- Expected vs actual behavior
- Environment details (OS, Python version, Docker version)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
