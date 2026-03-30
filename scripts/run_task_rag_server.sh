#!/usr/bin/env bash
set -euo pipefail
cd "${WORKSPACE:-$HOME/.openclaw/workspace}"
export WORKSPACE="${WORKSPACE:-$PWD}"

# load env
set -a
[ -f "$HOME/.openclaw/.env" ] && source "$HOME/.openclaw/.env" || true
set +a

source .venv-task-rag-server/bin/activate
exec uvicorn scripts.task_rag_server:app --host 0.0.0.0 --port 8711
