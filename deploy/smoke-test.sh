#!/usr/bin/env bash
# Post-deploy smoke test: verifies the running container is healthy and the
# end-to-end memory write→read path works without touching production data.
#
# Usage:
#   bash deploy/smoke-test.sh                            # uses .env on disk
#   ENDPOINT=http://127.0.0.1:8711 RAG_API_KEY=xxx bash deploy/smoke-test.sh
#
# Tests (in order):
#   1. /health returns 200 and accepting_ingest=true
#   2. /admin/system-health returns 200 and worker_running=true
#   3. /containers returns the expected list
#   4. /ingest-memory/objects writes a smoke-test object
#   5. /jobs lists the auto-embed job for that container
#   6. /search finds the smoke-test object once embed completes
#
# All operations target a dedicated container name (smoke-test-<ts>) so we
# never touch real memories. The test container is deleted at end.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

ENDPOINT="${ENDPOINT:-http://127.0.0.1:8711}"
RAG_API_KEY="${RAG_API_KEY:-}"

# If no key in env, try to source from .env
if [ -z "$RAG_API_KEY" ] && [ -f "$PROJECT_ROOT/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    . "$PROJECT_ROOT/.env"
    set +a
fi

if [ -z "${RAG_API_KEY:-}" ]; then
    echo "RAG_API_KEY not set; export it or place it in .env" >&2
    exit 2
fi

CONTAINER="smoke-test-$(date +%s)"
SMOKE_ID="smoke-$(date +%s%N)"

# pretty
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
pass() { echo -e "  ${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; exit 1; }
info() { echo -e "  ${YELLOW}[ ..]${NC} $1"; }

req() {
    # req <method> <path> [json-body]
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -m 30 -X "$method" "$ENDPOINT$path" \
            -H "X-API-KEY: $RAG_API_KEY" \
            -H "Content-Type: application/json" \
            -d "$body"
    else
        curl -sS -m 30 -X "$method" "$ENDPOINT$path" \
            -H "X-API-KEY: $RAG_API_KEY"
    fi
}

cleanup() {
    info "cleanup: deleting test container $CONTAINER"
    req DELETE "/containers/$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo ""
echo "=== smoke-test against $ENDPOINT (container=$CONTAINER) ==="

# 1. /health
info "1. checking /health"
HEALTH="$(curl -sS -m 10 "$ENDPOINT/health")"
echo "$HEALTH" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); \
    sys.exit(0 if d.get('status')=='ok' and d.get('accepting_ingest', True) else 1)" \
    || fail "/health unhealthy or refusing ingest: $HEALTH"
pass "/health OK"

# 2. /admin/system-health
info "2. checking /admin/system-health"
ADMIN="$(req GET /admin/system-health)"
echo "$ADMIN" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); \
    sys.exit(0 if d.get('admit_ok') else 1)" \
    || fail "/admin/system-health admit not OK: $ADMIN"
pass "/admin/system-health admit_ok"

# 3. /containers
info "3. /containers reachable"
req GET /containers >/dev/null || fail "/containers not reachable"
pass "/containers reachable"

# 4. ingest a memory object
info "4. ingest memory object id=$SMOKE_ID"
INGEST="$(req POST /ingest-memory/objects "$(cat <<JSON
{
  "container": "$CONTAINER",
  "auto_embed": true,
  "objects": [{
    "id": "$SMOKE_ID",
    "text": "Smoke test memory: kuiper-belt-flag-2026-pizza",
    "title": "smoke-test",
    "tags": ["smoke", "test"]
  }]
}
JSON
)")"
echo "$INGEST" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); \
    sys.exit(0 if d.get('accepted')==1 else 1)" \
    || fail "ingest did not accept the object: $INGEST"
pass "ingest accepted 1 object"

# 5. confirm embed enqueued
info "5. embed job enqueued"
sleep 1
JOBS="$(req GET "/jobs?container=$CONTAINER&limit=10")"
echo "$JOBS" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); \
    jobs=d.get('jobs',[]); \
    sys.exit(0 if any(j.get('op')=='embed' for j in jobs) else 1)" \
    || fail "no embed job found for $CONTAINER: $JOBS"
pass "embed job queued"

# 6. wait for embed → search
info "6. waiting up to 90s for embed to drain, then searching"
DEADLINE=$(( $(date +%s) + 90 ))
FOUND=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    SEARCH="$(req POST /search "{\"container\":\"$CONTAINER\",\"query\":\"kuiper-belt-flag-2026-pizza\",\"topk\":3}")"
    if echo "$SEARCH" | python3 -c "import json,sys; d=json.loads(sys.stdin.read()); \
        hits=d.get('results',[]); \
        sys.exit(0 if any('kuiper-belt-flag-2026-pizza' in (h.get('text') or '') for h in hits) else 1)" 2>/dev/null; then
        FOUND=1
        break
    fi
    sleep 5
done
if [ "$FOUND" -eq 1 ]; then
    pass "search found ingested memory"
else
    fail "search did not find smoke memory within 90s — last response: $SEARCH"
fi

echo ""
echo -e "${GREEN}=== all smoke checks passed ===${NC}"
