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
COOKIE_JAR="$(mktemp -t tm-smoke-cookie.XXXXXX)"

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
    rm -f "$COOKIE_JAR" 2>/dev/null || true
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

# 7-11. Admin dashboard session flow — catches the v0.17.0-class
# import-path bug where /admin/ui/* and /admin/usage/* return 500 even though
# /health and /search pass. Verifies the full login → authed GET → logout cycle.
info "7. /admin/ui/me unauth (expect 401)"
ME_UNAUTH=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$ENDPOINT/admin/ui/me" || true)
[ "$ME_UNAUTH" = "401" ] || fail "/admin/ui/me unauth expected 401, got $ME_UNAUTH"
pass "/admin/ui/me unauth -> 401"

info "8. /admin/ui/login"
LOGIN_BODY="$(mktemp -t tm-smoke-login.XXXXXX.json)"
LOGIN_CODE=$(curl -sS -m 10 -c "$COOKIE_JAR" -o "$LOGIN_BODY" -w '%{http_code}' \
    -X POST "$ENDPOINT/admin/ui/login" \
    -H "Content-Type: application/json" \
    -H "X-Requested-With: XMLHttpRequest" \
    --data "{\"api_key\":\"$RAG_API_KEY\"}")
if [ "$LOGIN_CODE" != "200" ]; then
    LOGIN_OUT="$(cat "$LOGIN_BODY")"
    rm -f "$LOGIN_BODY"
    fail "login expected 200, got $LOGIN_CODE: $LOGIN_OUT"
fi
rm -f "$LOGIN_BODY"
grep -q tm_sid "$COOKIE_JAR" || fail "tm_sid cookie not issued"
pass "/admin/ui/login -> 200 + tm_sid cookie"

info "9. /admin/usage/summary?window=24h (Lane B coverage)"
USAGE_CODE=$(curl -sS -m 10 -b "$COOKIE_JAR" -o /dev/null -w '%{http_code}' \
    "$ENDPOINT/admin/usage/summary?window=24h")
[ "$USAGE_CODE" = "200" ] || fail "GET /admin/usage/summary expected 200, got $USAGE_CODE"
pass "/admin/usage/summary -> 200"

info "10. /admin/ui/logout"
LOGOUT_CODE=$(curl -sS -m 10 -b "$COOKIE_JAR" -o /dev/null -w '%{http_code}' \
    -X POST "$ENDPOINT/admin/ui/logout" -H "X-Requested-With: XMLHttpRequest")
[ "$LOGOUT_CODE" = "200" ] || fail "logout expected 200, got $LOGOUT_CODE"
pass "/admin/ui/logout -> 200"

info "11. /admin/ui/me post-logout (expect 401)"
ME_POST=$(curl -sS -m 10 -b "$COOKIE_JAR" -o /dev/null -w '%{http_code}' "$ENDPOINT/admin/ui/me")
[ "$ME_POST" = "401" ] || fail "/admin/ui/me post-logout expected 401, got $ME_POST"
pass "/admin/ui/me post-logout -> 401"

# 12. Existing-container /search dim guard — catches the 2026-05-29 class of bug
# where EMBEDDING_DIM env drifts from a long-lived container's stored vector dim.
# Step 6 above uses a freshly created container (smoke-test-<ts>) so its stored
# vectors always match the current EMBEDDING_DIM by construction — it cannot
# expose runtime/storage dim drift. Here we pick the largest pre-existing
# production container and run a benign /search; an RuntimeError on dim mismatch
# would surface as a 500 with `query dim ... doesn't match column vector dim`.
info "12. /search against an existing production container (dim drift guard)"
CONTAINERS_JSON="$(req GET /containers)"
PROD_CONTAINER="$(echo "$CONTAINERS_JSON" | python3 -c "
import json, sys
d = json.load(sys.stdin)
items = d.get('containers') or d.get('data') or d if isinstance(d, list) else []
if isinstance(d, dict) and not items:
    items = list(d.values())[0] if d else []
candidates = []
for c in items:
    name = c if isinstance(c, str) else (c.get('name') or c.get('container') or '')
    if not name or name.startswith('smoke-test-'):
        continue
    candidates.append(name)
print(candidates[0] if candidates else '')
" 2>/dev/null)"
if [ -z "$PROD_CONTAINER" ]; then
    info "  no pre-existing production container found, skipping dim drift guard"
else
    DRIFT_RESP="$(req POST /search "{\"container\":\"$PROD_CONTAINER\",\"query\":\"smoke dim drift guard healthcheck\",\"topk\":1}")"
    if echo "$DRIFT_RESP" | grep -qiE "query dim|doesn't match.*vector dim|RuntimeError"; then
        fail "/search against $PROD_CONTAINER hit dim mismatch — EMBEDDING_DIM disagrees with stored vectors: $DRIFT_RESP"
    fi
    # Also reject any non-2xx-ish JSON ('detail' or 'error' keys signal trouble).
    if echo "$DRIFT_RESP" | python3 -c "
import json,sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)  # non-JSON, let curl status decide
sys.exit(2 if (d.get('detail') or d.get('error')) else 0)
" ; then
        :
    else
        rc=$?
        [ "$rc" = "2" ] && fail "/search against $PROD_CONTAINER returned error payload: $DRIFT_RESP"
    fi
    pass "/search against $PROD_CONTAINER reached LanceDB without dim mismatch"
fi

# 13. Frontend Playwright E2E assertions
info "13. running frontend Playwright E2E assertions"
if [ ! -d "$PROJECT_ROOT/dashboard/node_modules/@playwright/test" ]; then
    info "  installing E2E dependencies in dashboard..."
    pnpm --prefix "$PROJECT_ROOT/dashboard" install --prod=false
fi
TM_TEST_BASE="$ENDPOINT" TM_TEST_API_KEY="$RAG_API_KEY" TM_TEST_CONTAINER="$CONTAINER" \
  pnpm --prefix "$PROJECT_ROOT/dashboard" exec playwright test || fail "Frontend E2E test failed"
pass "Frontend E2E assertions passed"

# 14. Redis governance dep connectivity (blueprint P0). Redis is a SOFT
# dependency — the app degrades gracefully when it's down — so a failed ping
# WARNS but does NOT fail the smoke test. This only runs when a redis compose
# service is present (skipped on hosts that haven't adopted the redis service).
info "14. redis governance connectivity probe (soft)"
if command -v docker >/dev/null 2>&1 \
   && (cd "$PROJECT_ROOT" && docker compose ps --services 2>/dev/null | grep -qx redis); then
    if (cd "$PROJECT_ROOT" && docker compose exec -T redis redis-cli ping 2>/dev/null | grep -qi PONG); then
        pass "redis ping -> PONG"
    else
        info "  redis ping failed — app runs degraded (governance falls back to defaults); not failing smoke"
    fi
else
    info "  no redis compose service present — skipping (governance runs in default/degraded mode)"
fi

echo ""
echo -e "${GREEN}=== all smoke checks passed (14 steps: core + admin/ui + dim-drift guard + frontend E2E + redis probe) ===${NC}"

