#!/usr/bin/env bash
set -euo pipefail
CONFIG_FILE="${RAG_CONFIG_FILE:-$HOME/.config/transcendence-memory/rag-config.json}"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "RAG config not found: $CONFIG_FILE" >&2
  exit 1
fi

export RAG_CONFIG_FILE="$CONFIG_FILE"
export RAG_ENDPOINT=$(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding='utf-8'))['endpoint'])
PY
)
export RAG_AUTH_HEADER=$(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding='utf-8'))
print(cfg['auth']['name'])
PY
)
export RAG_API_KEY=$(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding='utf-8'))
print(cfg['auth']['value'])
PY
)
export RAG_DEFAULT_CONTAINER=$(python3 - "$CONFIG_FILE" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding='utf-8'))
print(cfg.get('defaultContainer', 'eva'))
PY
)
export RAG_AUTH_VALUE="$RAG_API_KEY"
echo "Loaded RAG config: $CONFIG_FILE" >&2
