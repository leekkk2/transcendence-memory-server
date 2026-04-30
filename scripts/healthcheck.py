#!/usr/bin/env python3
"""Stdlib-only liveness probe for the container.

Replaces curl in HEALTHCHECK so the runtime image stays slim. Exit 0 when
GET /health returns 200, exit 1 otherwise. The Docker daemon ignores stdout
on healthchecks, so we don't bother formatting output.
"""
import http.client
import os
import sys


def main() -> int:
    port = int(os.environ.get("TM_HEALTH_PORT", "8711"))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request("GET", "/health")
        resp = conn.getresponse()
        return 0 if resp.status == 200 else 1
    except Exception:
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
