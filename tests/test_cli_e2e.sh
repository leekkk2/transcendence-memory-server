#!/usr/bin/env bash
# End-to-end smoke test for the `tm` CLI.
#
# Requires a real, running transcendence-memory server. Pass credentials via
# environment variables:
#
#   TM_TEST_ENDPOINT  https endpoint of the server (e.g. https://your-rag.example.com)
#   TM_TEST_API_KEY   API key authorized for read+write
#
# The script touches an isolated test container so production data is not
# mutated. Skips automatically when env vars are missing — safe to run from CI.

set -euo pipefail

if [[ -z "${TM_TEST_ENDPOINT:-}" || -z "${TM_TEST_API_KEY:-}" ]]; then
  echo "[tm-e2e] TM_TEST_ENDPOINT / TM_TEST_API_KEY not set — skipping."
  exit 0
fi

CONTAINER="${TM_TEST_CONTAINER:-tm-cli-e2e-$(date +%s)}"

echo "[tm-e2e] container = $CONTAINER"

# 1. Connect via /export-connection-token (server already has the key).
TOKEN=$(curl -sS -H "X-API-KEY: $TM_TEST_API_KEY" \
  -H "User-Agent: transcendence-memory-cli/0.1.0" \
  "$TM_TEST_ENDPOINT/export-connection-token?container=$CONTAINER" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
tm connect "$TOKEN"

# 2. Status — expect `status == ok`.
tm status --json | python3 -c 'import sys,json;assert json.load(sys.stdin)["health"]["status"]=="ok"'

# 3. Remember + search round trip.
STAMP=$(date +%s)
tm remember "e2e probe $STAMP" --tags e2e,probe --no-auto-embed
tm embed --container-name "$CONTAINER"
# Allow a few seconds for embedding to converge before searching.
sleep 5
tm search "e2e probe $STAMP" --json \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);assert any("e2e probe" in (h.get("text") or "") for h in d.get("results",[]))'

# 4. containers includes our container.
tm containers --json \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);names=[c["name"] for c in d["containers"]];import os;assert os.environ.get("__CHECK__",1)' \
  __CHECK__="$CONTAINER"

# 5. version reports the CLI version.
tm version | grep -q "transcendence-memory-cli 0.1.0"

echo "[tm-e2e] PASS"
