#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SERVER_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
DEFAULT_WORKSPACE="$SERVER_ROOT"
WORKSPACE_VALUE="${WORKSPACE:-$DEFAULT_WORKSPACE}"
cd "$SERVER_ROOT"
export WORKSPACE="$WORKSPACE_VALUE"

# load env
set -a
[ -f "$SERVER_ROOT/.env" ] && source "$SERVER_ROOT/.env" || true
set +a

source .venv-task-rag-server/bin/activate
exec uvicorn scripts.task_rag_server:app --host 0.0.0.0 --port 8711
