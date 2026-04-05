# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public GitHub issue
2. Use [GitHub Security Advisories](https://github.com/leekkk2/transcendence-memory-server/security/advisories/new) to report privately
3. Include steps to reproduce, impact assessment, and suggested fix if possible

We will acknowledge receipt within 48 hours and aim to release a fix within 7 days for critical issues.

## Scope

- API authentication bypass
- Remote code execution
- Path traversal / directory traversal
- Information disclosure
- Denial of service

## Best Practices for Deployment

- Always set `RAG_API_KEY` in production
- Use HTTPS (reverse proxy with TLS termination)
- Restrict Docker port binding to `127.0.0.1`
- Keep dependencies updated
