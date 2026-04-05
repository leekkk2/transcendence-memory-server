#!/bin/bash
# transcendence-memory-server 部署预检脚本
# 用法：bash scripts/preflight_check.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
PASS=0
FAIL=0
WARN=0

pass() { echo -e "  ${GREEN}[PASS]${NC} $1"; ((PASS++)); }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; ((FAIL++)); }
warn() { echo -e "  ${YELLOW}[WARN]${NC} $1"; ((WARN++)); }

echo ""
echo "=== Transcendence Memory Server — Preflight Check ==="
echo ""

# 1. Docker daemon
if docker info &>/dev/null; then
    pass "Docker daemon is accessible"
elif sudo docker info &>/dev/null; then
    warn "Docker daemon requires sudo (consider adding user to docker group)"
else
    fail "Docker daemon is not accessible"
fi

# 2. Docker Compose
if docker compose version &>/dev/null; then
    pass "Docker Compose v2 available"
elif docker-compose version &>/dev/null; then
    warn "Only docker-compose v1 found (v2 recommended)"
else
    fail "Docker Compose not found"
fi

# 3. .env 文件
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

check_key() {
    local key=$1 level=$2
    local desc="${3:-}"
    if grep -q "^${key}=" "$ENV_FILE" && [ -n "$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)" ]; then
        val=$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)
        if echo "$val" | grep -qE '^(your-|sk-your)'; then
            fail "$key is still placeholder value"
        else
            pass "$key is configured"
        fi
    else
        if [ "$level" = "required" ]; then
            fail "$key is missing or empty (required)"
        else
            warn "$key is not set ($desc)"
        fi
    fi
}

if [ -f "$ENV_FILE" ]; then
    pass ".env file exists"
    check_key "RAG_API_KEY" "required"
    check_key "EMBEDDING_API_KEY" "required"
    check_key "LLM_API_KEY" "optional" "LightRAG knowledge graph disabled"
    check_key "VLM_API_KEY" "optional" "Multimodal RAG disabled"
else
    fail ".env file not found at $ENV_FILE (copy from .env.example)"
fi

# 4. 端口检查
TM_PORT="${TM_PORT:-8711}"
if command -v lsof &>/dev/null; then
    if lsof -i ":${TM_PORT}" &>/dev/null; then
        fail "Port ${TM_PORT} is already in use"
    else
        pass "Port ${TM_PORT} is available"
    fi
elif command -v ss &>/dev/null; then
    if ss -tlnp | grep -q ":${TM_PORT} "; then
        fail "Port ${TM_PORT} is already in use"
    else
        pass "Port ${TM_PORT} is available"
    fi
else
    warn "Cannot check port (lsof/ss not found)"
fi

# 结果汇总
echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed, ${WARN} warnings ==="
if [ "$FAIL" -gt 0 ]; then
    echo -e "${RED}Fix the failures above before deploying.${NC}"
    exit 1
else
    echo -e "${GREEN}Ready to deploy!${NC}"
    echo ""
    echo "Next steps:"
    echo "  docker compose up -d --build                                                    # 开发/测试"
    echo "  docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build   # 生产"
fi
