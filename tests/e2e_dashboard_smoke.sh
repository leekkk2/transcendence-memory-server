#!/usr/bin/env bash
# E2E smoke for the admin dashboard endpoints. Exercises the full /admin/ui/*
# lifecycle against a live server: 401 → login → cookie-authenticated GETs
# across the dashboard's data sources → logout → 401 again.
#
# Required env:
#   TM_TEST_BASE      — base URL of the running server (e.g. https://your-rag.example.com)
#   TM_TEST_API_KEY   — value of the server's RAG_API_KEY
#
# Optional env:
#   TM_TEST_VERBOSE=1 — echo every curl status line for debugging
#
# Exit code: 0 = pass; non-zero = the failing step is the last line printed.
set -euo pipefail

BASE="${TM_TEST_BASE:?TM_TEST_BASE required, e.g. https://your-rag.example.com}"
KEY="${TM_TEST_API_KEY:?TM_TEST_API_KEY required}"
COOKIE_JAR="$(mktemp -t tm-dash-cookie.XXXXXX)"
trap 'rm -f "$COOKIE_JAR"' EXIT

log() { [[ "${TM_TEST_VERBOSE:-0}" == 1 ]] && echo "[smoke] $*" >&2 || true; }
fail() { echo "[smoke][FAIL] $*" >&2; exit 1; }

# 1. Unauthenticated /me must return 401 (the SPA uses this to detect login).
status=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE/admin/ui/me" || true)
log "step 1 /me unauth → $status"
[[ "$status" == "401" ]] || fail "unauthenticated /admin/ui/me expected 401, got $status"

# 2. POST /admin/ui/login with the correct api key — server should respond
#    200 with a Set-Cookie that we capture into the jar for subsequent requests.
status=$(curl -sS -c "$COOKIE_JAR" -o /tmp/tm-login-body.json -w "%{http_code}" \
  -X POST "$BASE/admin/ui/login" \
  -H "Content-Type: application/json" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data "{\"api_key\":\"$KEY\"}")
log "step 2 login → $status"
[[ "$status" == "200" ]] || fail "login expected 200, got $status (body: $(cat /tmp/tm-login-body.json))"
grep -q 'tm_sid' "$COOKIE_JAR" || fail "tm_sid cookie not issued"

# 3. Authenticated GETs across the five core dashboard data sources. Each
#    response must be valid JSON; we don't assert keys (server schema may grow)
#    but a non-JSON / 5xx will trip jq.
for path in \
    /health \
    /admin/system-health \
    "/admin/usage/summary?window=24h" \
    /containers \
    /jobs ; do
  status=$(curl -sS -b "$COOKIE_JAR" -o /tmp/tm-resp.json -w "%{http_code}" "$BASE$path")
  log "step 3 GET $path → $status"
  [[ "$status" == "200" ]] || fail "GET $path expected 200, got $status"
  # /health is unauthenticated but still must be JSON; /admin/usage/* may 404
  # if Lane B is not yet deployed — tolerate it but warn.
  if ! jq -e . /tmp/tm-resp.json >/dev/null 2>&1; then
    fail "non-json response from $path"
  fi
done

# 4. Logout — POST /admin/ui/logout invalidates the server-side session.
status=$(curl -sS -b "$COOKIE_JAR" -o /dev/null -w "%{http_code}" \
  -X POST "$BASE/admin/ui/logout" \
  -H "X-Requested-With: XMLHttpRequest")
log "step 4 logout → $status"
[[ "$status" == "200" ]] || fail "logout expected 200, got $status"

# 5. After logout the same cookie value must no longer authenticate.
status=$(curl -sS -b "$COOKIE_JAR" -o /dev/null -w "%{http_code}" "$BASE/admin/ui/me")
log "step 5 /me post-logout → $status"
[[ "$status" == "401" ]] || fail "expected 401 after logout, got $status"

echo "[smoke] dashboard E2E passed"
