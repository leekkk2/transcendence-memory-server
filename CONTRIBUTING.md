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
